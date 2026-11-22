import unittest
from unittest.mock import PropertyMock, patch

from scale_build.packages import order
from scale_build.packages.package import BinaryPackage, Package


def dependency_graph():
    dependencies = {
        'truenas': {'truenas_samba'},
        'truenas_samba': {'openzfs'},
        'openzfs': {'kernel', 'kernel-dbg'},
        'kernel': set(),
        'kernel-dbg': set(),
        'unrelated': {'kernel'},
    }
    packages = []
    for name, deps in dependencies.items():
        package = Package(name, 'main', 'https://example.invalid/source', explicit_deps=deps)
        # Model source-name dependencies that cannot be discovered through a
        # matching binary package name (including OpenZFS virtual Provides).
        package._binary_packages = [BinaryPackage(name + '-binary', set(), name, name, set())]
        packages.append(package)
    return packages


class SelectedPackageDependencyTests(unittest.TestCase):
    def select(self, changed):
        with patch.object(order, 'get_packages', return_value=dependency_graph()), patch.object(
            Package, 'exists', new_callable=PropertyMock, return_value=True,
        ), patch.object(Package, '_hash_changed', autospec=True, side_effect=lambda package: package.name in changed):
            packages = order.get_initialized_packages(['truenas'])
            selected = order.get_to_build_packages(packages, ['truenas'])
        return packages, selected

    def test_selected_root_includes_changed_transitive_dependencies(self):
        packages, selected = self.select({'truenas_samba', 'openzfs', 'kernel', 'kernel-dbg', 'unrelated'})
        self.assertEqual(set(selected), {'truenas', 'truenas_samba', 'openzfs', 'kernel', 'kernel-dbg'})
        self.assertTrue(packages['truenas'].force_build)
        self.assertFalse(packages['unrelated'].force_build)

    def test_retry_reuses_successful_unchanged_kernels(self):
        packages, selected = self.select({'truenas_samba', 'openzfs'})
        self.assertEqual(set(selected), {'truenas', 'truenas_samba', 'openzfs'})
        self.assertFalse(packages['kernel'].force_build)
        self.assertFalse(packages['kernel-dbg'].force_build)

    def test_changed_ancestor_rebuilds_cached_intermediate_dependents_only(self):
        packages, selected = self.select({'kernel', 'unrelated'})
        self.assertEqual(set(selected), {'truenas', 'truenas_samba', 'openzfs', 'kernel'})
        self.assertFalse(packages['kernel-dbg'].force_build)
        self.assertFalse(packages['unrelated'].parent_changed)
