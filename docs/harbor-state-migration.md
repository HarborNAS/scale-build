# Harbor state across native boot-environment upgrades

`truenas_install.harbor_state` preserves the explicitly reviewed Harbor state
when the native `.update` installer creates a new boot environment (BE). It is
not an in-place middleware override. Fresh installs and systems without Harbor
state retain the existing installer behavior.

## Native package upgrade policy

A new immutable Nexus lock can select
`scope.service_migration = "native-packages-v1"`. The image builder carries this
policy into the update manifest; it does not change historical locks. Without
this opt-in, the retained Demo compatibility path below remains in effect.

The native policy saves the new image's packaged Beacon/Gate units before
copying stopped source state. Only in the new BE, it archives the old units,
their drop-ins, enablement links and four legacy environment files under
`/var/lib/harboros-upgrade/native-legacy-config` (private parent mode `0700`).
The original BE is untouched. The new BE restores the exact package units and
enables `/usr/bin/harboros-beacon` and `/usr/bin/harboros-im-gate`; the old Python
edge, nginx watcher/path and compatibility drop-in are not active.

Business/model configuration is extracted from the old units and reviewed
`/etc/default` files without executing shell input. Native unit environment
values take precedence: product/runtime profiles, loopback bindings, state
directories, southbound mode and credential paths cannot be replaced by old
Demo values. Legacy directional service tokens and credential-path overrides are
archived rather than placed in the active environment. Gate's first-boot
credential recovery and Beacon's model-token helper prepare the native
credentials. The existing private model API token is retained for saved model
clients, subject to the package helper's directional-token isolation check.
Feishu configuration and mail credentials remain private in the
new Gate environment. The old edge workspace ID becomes Gate's authoritative
workspace ID when no explicit Gate workspace exists, preserving the existing
Beacon state scope.

Gate's sessions, media, setup portal and `/var/lib/harborgate` mail-token state
are also copied into `/var/lib/harboros-im-gate`, the native sandbox's writable
root. Conflicting existing Gate files fail the migration rather than selecting
one silently. The native `/data` clones remain intact for old references.
Beacon's copied state and boot-BE knowledge/model tree keep their bytes and
paths, with new-BE ownership changed to the packaged `harborbeacon` account.
Independent data-pool models and binaries are never chowned or copied by this
policy. External `/mnt/...` configuration references found in the retained
Beacon environment and administration state are recorded in the receipt as
`external_access_requirements`, with the target account/UID/GID and status
`pending_target_permission_migration`. No external ACLs are changed. Their
parent traversal and read/write/execute access require a separate, explicit
application-directory permission migration and actual post-boot acceptance;
NAS SMB data permissions are outside this conversion.

The existing pre-activation receipt records archived originals, removed target
paths, state relocations, new target hashes/owners and the native policy. Both
the stopped source and the transformed target must still match that plan
before bootfs selection. This source change is not evidence that a real
upgrade, restart or rollback has passed.

Fresh installation (`old_root=None`) does not import Demo units, environment,
state or credentials. It uses the package units and device credential helpers
at first boot. Model weights and data-pool business configuration are separate
from the OS image; fresh boot does not imply those AI resources are provisioned.

## Copy and verification boundary

The migration copies only these existing source paths into the new BE:

* `/var/lib/harboros-beacon`, including administration configuration, device
  registry, conversation ledger, image turns and document turns.
* `/var/lib/harboros-im-gate` and `/var/lib/harbor-services`.
* `/mnt/software/harborbeacon-agent-ci`, the reviewed knowledge documents,
  knowledge index, model store and supporting files on the old BE's `/mnt`
  dataset. Preserve `/mnt/software` metadata without copying its other children.
* `/mnt/software/harbor-apps/ops`, including its releases and its reviewed
  `current` link. Preserve its parent directory metadata without copying sibling
  applications. The absent legacy `/var/lib/harbor/apps/ops` is not synthesized.
* Four named Harbor environment files under `/etc/default`.
* The named Beacon, Gate, edge, model, photo and subscreen units, their three
  reviewed drop-in directories, the nginx regeneration path unit, and the
  W7900 runtime-power rule. The exact list is in `CONFIG_PATHS`.
* Only the matching `multi-user.target.wants` links in `ENABLEMENT_PATHS`.
  Source enablement is preserved, including disabled units. Links must point
  to the corresponding reviewed unit path, and the unit must exist in the new
  BE. Unrelated systemd enablement is not changed.

The native installer already clones `/data`. The migration separately verifies
`/data/sessions`, `/data/media-inbound`, `/data/media-outbound`,
`/data/_setup_portal.json` and `/data/harborbeacon`. Their bytes and numeric
owners must match the stopped source. The installer's existing recursive
`u=rwX,g=,o=` permission hardening is expected in the target; migration does not
weaken those permissions to reproduce older modes.

There is no whole-`/etc` copy, no old `/usr` overlay, and no replacement of the
new middleware or its generated `/etc/nginx/nginx.conf`. Models, release
binaries, credentials and live media on independent data-pool datasets remain
in place. Files living in the old BE's `/mnt` dataset are **not** automatically
persistent merely because their paths begin with `/mnt`; every such dependency
needs an explicit reviewed migration scope.

## Consistency and ownership

Before the installer takes any `/data` clone, all listed Harbor services and
the path unit must be inactive or failed, with zero main/control PIDs. The
installer checks but never stops services itself. The operator must close
ingress and stop activation for the maintenance window. Rechecking hashes does
not replace stopping writers.

The active source BE must also be the currently selected pool bootfs; an
unrelated pending boot selection is not overwritten. The source root and
target root must identify different BEs. `/etc`, `/var/lib`,
`/data` and, when needed, `/mnt` must be mounted from each declared BE's
corresponding ZFS dataset. An intervening mount hiding a selected tree is also
rejected, so an independent pool cannot be mistaken for boot-environment data.
Unknown nested mounts, special files and unreviewed symlinks fail closed.
The explicit link exceptions are systemd enablement, the two observed
HuggingFace repositories' named snapshot files pointing to ordinary files in
their own `blobs` directories, and the Ops `current` link pointing to an ordinary
`releases/harborops-*` directory in the same managed root. Target files and
release trees must themselves occur in the copied inventory. Link chains,
broken links, unknown directory links and escapes are rejected. Absolute Ops
links are resolved against the relevant BE root, never the installer's running
host root.
Parent symlinks are never traversed. Source and destination files are compared
using SHA-256, mode, numeric UID/GID and type. Approved links are compared as
links, without following their content. `rsync --numeric-ids` preserves owners;
the migration does not create accounts or overwrite passwd/group files.

The source is captured before native cloning, checked after copying, and
checked again immediately before the installer selects the new bootfs.
Destination equality is checked at both points. Any failure uses the existing
installer exception path: restore the old bootfs and destroy the failed new BE
when cleanup is enabled. The module never restores or edits old business state.

A successful pre-activation check creates
`/var/lib/harboros-upgrade/state-migration.json`, with directory mode `0700` and
file mode `0600`. It records source/target BE identities and content hashes, not
file contents or credential values. Treat the receipt as private because paths
can contain session identifiers. Preserve it with the rollout evidence and
compare it with the first boot before reopening ingress.

## Rollout and rollback checks

1. Verify exact source pins and artifact digest, retain the original BE with
   `keep=true`, and record its name and current bootfs. Check available boot-pool
   space and health before starting. This module does not choose a release.
2. Verify every external service, model, media and credential reference on the
   data pool, and verify the service accounts from the preserved configuration.
   The observed demo runs Beacon/Gate as root and model services as
   `harboros_admin` (UID/GID 950); numeric ownership alone does not prove that
   first-boot account regeneration has completed.
3. Close ingress, drain turns/deliveries, stop Harbor services and path
   activation, then invoke the native manual update. Keep writers stopped until
   the install and reboot complete. On pre-activation failure, confirm the old
   bootfs is restored before restarting the original services.
4. On the new BE, verify the receipt, runtime unit configuration, account
   identities, session/conversation/turn state and external library fingerprints.
   Check storage APIs/UI, service health, authenticated UI routing, PDF upload
   and archive, and existing image retrieval before reopening ingress.
5. If acceptance fails, keep ingress closed and stop the new writers before
   selecting the retained old BE and rebooting. Confirm its original binaries,
   configuration and state fingerprints. Switching BEs does not roll back
   independent media pools, and no pool snapshot should be restored over newer
   user data. If the new BE accepted real writes, preserve and reconcile those
   writes before rollback; boot selection alone does not merge them into the old
   BE. The migration is not an automatic cross-BE business-state rollback tool.

## Compatibility gates for the current demo

The observed legacy photo nginx helper requires exactly one unmodified
`location /api/harbor-beacon` block and rewrites it to port 4176. The formal
middleware template at commit `7951ea0939f0aac21ef9d755bbc3347fe6a78747` instead
has exact/prefix locations routed through Gate on port 8787 with authentication
header filtering. Calling the existing helper against that template raises
`Expected exactly one Harbor Beacon nginx location`.

The old watcher is not compatible with that template. The explicit new-BE
adapter preserves the retained Demo edge's NAS-user authentication on port 4176,
retains principal-header stripping and disables the legacy external Gate
setup/control routes. It does not claim that the retained Gate implements the
new formal Gate contract. Its compatibility mode is recorded as
`retained-demo-edge-v1-external-gate-disabled`. The original watcher in the old
BE and the existing r4 release stay unchanged. The reviewed new watcher,
standalone helper and
`/etc/systemd/system/nginx.service.d/40-harbor-demo-compat.conf` are recorded as
target transformations rather than equal source copies. The nginx drop-in runs
the adapter synchronously before the native start test and reload command, so
the new template is adapted before nginx can serve it; the existing path watcher
is retained as a fallback. The helper releases its lock before requesting a
reload to avoid recursively blocking the synchronous hook. Verify actual
generated nginx configuration and start/reload behavior in acceptance. Do not
copy an old nginx configuration over the new template.

The explicit boot-BE knowledge/model root is about 2.8 GB on the observed demo;
include that size and an appropriate margin in the update free-space precheck.
The additional reviewed Ops root is about 11 MB. These compatibility and
first-boot gates are not satisfied by the file-copy tests below.

## Regression checks

Run `python -m unittest truenas_install.tests.test_harbor_state -v` on Linux
with `rsync` available. It covers state and attachment preservation, native
session-clone mismatch, concurrent changes, destination corruption, wrong BE
mounts, nested mounts, path traversal, symlink rejection, special files, stopped
writers, permission hardening, actual rsync and enablement, and retention of
untouched source state when a failed new BE is discarded.

The existing CI suite also imports these tests through
`scale_build/tests/unit/test_harbor_state_migration.py`. Windows can run the
portable tests; POSIX ownership/mode, symlink, FIFO and real
rsync checks must pass on the Linux build host before publication. These tests
do not claim a real appliance upgrade/reboot/rollback has been executed.
