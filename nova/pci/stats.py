# Copyright (c) 2013 Intel, Inc.
# Copyright (c) 2013 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy

from oslo_log import log as logging
import six

from nova import exception
from nova.i18n import _LE
from nova.objects import pci_device_pool
from nova.pci import utils
from nova.pci import whitelist


LOG = logging.getLogger(__name__)


class PciDeviceStats(object):

    """PCI devices summary information.

    According to the PCI SR-IOV spec, a PCI physical function can have up to
    256 PCI virtual functions, thus the number of assignable PCI functions in
    a cloud can be big. The scheduler needs to know all device availability
    information in order to determine which compute hosts can support a PCI
    request. Passing individual virtual device information to the scheduler
    does not scale, so we provide summary information.

    Usually the virtual functions provided by a host PCI device have the same
    value for most properties, like vendor_id, product_id and class type.
    The PCI stats class summarizes this information for the scheduler.

    The pci stats information is maintained exclusively by compute node
    resource tracker and updated to database. The scheduler fetches the
    information and selects the compute node accordingly. If a comptue
    node is selected, the resource tracker allocates the devices to the
    instance and updates the pci stats information.

    This summary information will be helpful for cloud management also.
    """

    pool_keys = ['product_id', 'vendor_id', 'numa_node']

    def __init__(self, stats=None):
        super(PciDeviceStats, self).__init__()
        # NOTE(sbauza): Stats are a PCIDevicePoolList object
        self.pools = [pci_pool.to_dict()
                      for pci_pool in stats] if stats else []
        self.pools.sort(key=lambda item: len(item))

    def _equal_properties(self, dev, entry, matching_keys):
        return all(dev.get(prop) == entry.get(prop)
                   for prop in matching_keys)

    def _find_pool(self, dev_pool):
        """Return the first pool that matches dev."""
        for pool in self.pools:
            pool_keys = pool.copy()
            del pool_keys['count']
            del pool_keys['devices']
            if (len(pool_keys.keys()) == len(dev_pool.keys()) and
                self._equal_properties(dev_pool, pool_keys, dev_pool.keys())):
                return pool

    def _create_pool_keys_from_dev(self, dev):
        """create a stats pool dict that this dev is supposed to be part of

        Note that this pool dict contains the stats pool's keys and their
        values. 'count' and 'devices' are not included.
        """
        # Don't add a device that doesn't have a matching device spec.
        # This can happen during initial sync up with the controller
        devspec = whitelist.get_pci_device_devspec(dev)
        if not devspec:
            return
        tags = devspec.get_tags()
        pool = {k: getattr(dev, k) for k in self.pool_keys}
        if tags:
            pool.update(tags)
        return pool

    def add_device(self, dev):
        """Add a device to its matching pool."""
        dev_pool = self._create_pool_keys_from_dev(dev)
        if dev_pool:
            pool = self._find_pool(dev_pool)
            if not pool:
                dev_pool['count'] = 0
                dev_pool['devices'] = []
                self.pools.append(dev_pool)
                self.pools.sort(key=lambda item: len(item))
                pool = dev_pool
            pool['count'] += 1
            pool['devices'].append(dev)

    @staticmethod
    def _decrease_pool_count(pool_list, pool, count=1):
        """Decrement pool's size by count.

        If pool becomes empty, remove pool from pool_list.
        """
        if pool['count'] > count:
            pool['count'] -= count
            count = 0
        else:
            count -= pool['count']
            pool_list.remove(pool)
        return count

    def remove_device(self, dev):
        """Remove one device from the first pool that it matches."""
        dev_pool = self._create_pool_keys_from_dev(dev)
        if dev_pool:
            pool = self._find_pool(dev_pool)
            if not pool:
                raise exception.PciDevicePoolEmpty(
                    compute_node_id=dev.compute_node_id, address=dev.address)
            pool['devices'].remove(dev)
            self._decrease_pool_count(self.pools, pool)

    def get_free_devs(self):
        free_devs = []
        for pool in self.pools:
            free_devs.extend(pool['devices'])
        return free_devs

    def consume_requests(self, pci_requests, numa_cells=None):
        alloc_devices = []
        for request in pci_requests:
            count = request.count
            spec = request.spec
            # For now, keep the same algorithm as during scheduling:
            # a spec may be able to match multiple pools.
            pools = self._filter_pools_for_spec(self.pools, spec)
            if numa_cells:
                pools = self._filter_pools_for_numa_cells(pools, numa_cells)
            # Failed to allocate the required number of devices
            # Return the devices already allocated back to their pools
            if sum([pool['count'] for pool in pools]) < count:
                LOG.error(_LE("Failed to allocate PCI devices for instance."
                          " Unassigning devices back to pools."
                          " This should not happen, since the scheduler"
                          " should have accurate information, and allocation"
                          " during claims is controlled via a hold"
                          " on the compute node semaphore"))
                for d in range(len(alloc_devices)):
                    self.add_device(alloc_devices.pop())
                return None
            for pool in pools:
                if pool['count'] >= count:
                    num_alloc = count
                else:
                    num_alloc = pool['count']
                count -= num_alloc
                pool['count'] -= num_alloc
                for d in range(num_alloc):
                    pci_dev = pool['devices'].pop()
                    pci_dev.request_id = request.request_id
                    alloc_devices.append(pci_dev)
                if count == 0:
                    break
        return alloc_devices

    @staticmethod
    def _filter_pools_for_spec(pools, request_specs):
        return [pool for pool in pools
                if utils.pci_device_prop_match(pool, request_specs)]

    @staticmethod
    def _filter_pools_for_numa_cells(pools, numa_cells):
        # Some systems don't report numa node info for pci devices, in
        # that case None is reported in pci_device.numa_node, by adding None
        # to numa_cells we allow assigning those devices to instances with
        # numa topology
        numa_cells = [None] + [cell.id for cell in numa_cells]
        # filter out pools which numa_node is not included in numa_cells
        return [pool for pool in pools if any(utils.pci_device_prop_match(
                                pool, [{'numa_node': cell}])
                                              for cell in numa_cells)]

    def _apply_request(self, pools, request, numa_cells=None):
        count = request.count
        matching_pools = self._filter_pools_for_spec(pools, request.spec)
        if numa_cells:
            matching_pools = self._filter_pools_for_numa_cells(matching_pools,
                                                          numa_cells)
        if sum([pool['count'] for pool in matching_pools]) < count:
            return False
        else:
            for pool in matching_pools:
                count = self._decrease_pool_count(pools, pool, count)
                if not count:
                    break
        return True

    def support_requests(self, requests, numa_cells=None):
        """Check if the pci requests can be met.

        Scheduler checks compute node's PCI stats to decide if an
        instance can be scheduled into the node. Support does not
        mean real allocation.
        If numa_cells is provided then only devices contained in
        those nodes are considered.
        """
        # note (yjiang5): this function has high possibility to fail,
        # so no exception should be triggered for performance reason.
        pools = copy.deepcopy(self.pools)
        return all([self._apply_request(pools, r, numa_cells)
                        for r in requests])

    def apply_requests(self, requests, numa_cells=None):
        """Apply PCI requests to the PCI stats.

        This is used in multiple instance creation, when the scheduler has to
        maintain how the resources are consumed by the instances.
        If numa_cells is provided then only devices contained in
        those nodes are considered.
        """
        if not all([self._apply_request(self.pools, r, numa_cells)
                                            for r in requests]):
            raise exception.PciDeviceRequestFailed(requests=requests)

    def __iter__(self):
        # 'devices' shouldn't be part of stats
        pools = []
        for pool in self.pools:
            tmp = {k: v for k, v in six.iteritems(pool) if k != 'devices'}
            pools.append(tmp)
        return iter(pools)

    def clear(self):
        """Clear all the stats maintained."""
        self.pools = []

    def __eq__(self, other):
        return cmp(self.pools, other.pools) == 0

    def __ne__(self, other):
        return not (self == other)

    def to_device_pools_obj(self):
        """Return the contents of the pools as a PciDevicePoolList object."""
        stats = [x for x in self]
        return pci_device_pool.from_pci_stats(stats)
