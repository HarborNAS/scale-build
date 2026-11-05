import os
import unittest
from unittest.mock import patch

from scale_build.product_profiles import selected_profile, external_package_url

class ProductProfileTests(unittest.TestCase):
    def test_nexus_profile_requires_amd64_and_immutable_component_versions(self):
        with patch.dict(os.environ, {'HARBOROS_PRODUCT':'nexus','HARBOROS_ARCHITECTURE':'amd64','HARBOROS_BEACON_VERSION':'1.1.0+nexus.amd64.test'}, clear=False):
            profile=selected_profile()
            self.assertEqual(profile['architecture'],'amd64')
            url=external_package_url('harboros-beacon', {'deb_version':'1.0.0','url_template':'unused'})
            self.assertIn('1.1.0+nexus.amd64.test',url)

    def test_n2_is_not_assembled_by_scale_build(self):
        with patch.dict(os.environ, {'HARBOROS_PRODUCT':'n2','HARBOROS_ARCHITECTURE':'riscv64'}, clear=False):
            with self.assertRaisesRegex(Exception, 'not scale-build'):
                selected_profile()

if __name__ == '__main__': unittest.main()
