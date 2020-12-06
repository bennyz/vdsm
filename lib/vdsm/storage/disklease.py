import collections
import json
import logging

from vdsm.storage import clusterlock
from vdsm.storage import guarded
from vdsm.storage import resourceManager as rm
from vdsm.storage import constants as sc
from vdsm.storage.constants import STORAGE
from vdsm.storage.sdc import sdCache
from vdsm.storage.compat import sanlock
from vdsm.storage import exception as se

Lease = collections.namedtuple("Lease", "name, path, offset")

# Cannot tell because clusterlock does not implement this or call failed.
HOST_STATUS_UNAVAILABLE = "unavailable"

# Host has a lease on the storage, but the clusterlock cannot tell if the host
# is live or dead yet. Would typically last for 10-20 seconds, but it's
# possible that this could persist for up to 80 seconds before host is
# considered live or fail.
HOST_STATUS_UNKNOWN = "unknown"

# There is no lease for this host id.
HOST_STATUS_FREE = "free"

# Host has renewed its lease in the last 80 seconds. It may be renewing its
# lease now or not, we can tell that only by checking again later.
HOST_STATUS_LIVE = "live"

# Host has not renewed its lease for 80 seconds. Would last for 60 seconds
# before host is considered dead.
HOST_STATUS_FAIL = "fail"

# Host has not renewed its lease for 140 seconds.
HOST_STATUS_DEAD = "dead"


class DiskLease(guarded.AbstractLock):

    log = logging.getLogger('storage.disklease')

    def __init__(self, resource, path, offset, sd_id, host_id):
        self._lease = Lease(resource, path, offset)
        self._sd_id = sd_id
        self._host_id = host_id
        self._generation = {'gen': '0'}
        self._sanlock_fd = None

    @property
    def ns(self):
        return rm.getNamespace(sc.DISK_LEASE_NAMESPACE, self._sd_id)

    @property
    def name(self):
        return self._lease.name

    @property
    def mode(self):
        return rm.EXCLUSIVE

    def acquire(self):
        try:
            # check is none
            self._sanlock_fd = sanlock.register()
        except sanlock.SanlockException as e:
            raise se.AcquireLockFailure(
                self._sdUUID, e.errno,
                "Cannot register to sanlock", str(e))

        resource_name = self._lease.name.encode("utf-8")
        try:
            sanlock.acquire(self._sd_id.encode("utf-8"), resource_name,
                            [(self._lease.path, self._lease.offset)],
                            slkfd=self._sanlock_fd,
                            lvb=True)
        except sanlock.SanlockException as e:
            raise se.AcquireLockFailure(
                    self._sdUUID, e.errno,
                    "Cannot acquire %s" % (self._lease,), str(e))
        self.set_lvb(self._lease, json.dumps(self._generation).encode("utf-8"))

    def release(self):
        resource_name = self._lease.name.encode("utf-8")
        try:
            sanlock.release(self._sd_id.encode("utf-8"), resource_name,
                            [(self._lease.path, self._lease.offset)],
                            slkfd=self._sanlock_fd)
        except sanlock.SanlockException as e:
            raise se.ReleaseLockFailure(self._sdUUID, e)

    def set_lvb(self, lease, data):
        try:
            sanlock.set_lvb(self._sd_id.encode("utf-8"),
                            lease.name.encode("utf-8"),
                            [(lease.path, lease.offset)],
                            data)
        except sanlock.SanlockException as e:
            raise se.ReleaseLockFailure(self._sdUUID, e)

    def get_lvb(self, lease):
        try:
            data = sanlock.get_lvb(self._sd_id.encode("utf-8"),
                                   lease.name.encode("utf-8"),
                                   [(lease.path, lease.offset)])
            return json.loads(data.encode("utf-8"))
        except sanlock.SanlockException as e:
            raise se.ReleaseLockFailure(self._sdUUID, e)

    def _make_lock(self, sd_id):
        with rm.acquireResource(STORAGE, sd_id, rm.SHARED):
            dom = sdCache.produce_manifest(sd_id)
            return clusterlock.SANLock(sd_id, dom.external_leases_path(), None, None)

    def update_generation(self, value):
        self.log.info("updating generation from %s to %s", self._generation['gen'], self._generation['gen'] + str(value))
        self._generation['gen'] = int(self._generation['gen']) + value
        self.set_lvb(self._lease, json.dumps(self._generation).encode("utf-8"))
