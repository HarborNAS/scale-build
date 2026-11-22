import os
import unittest
from unittest.mock import Mock, patch

import requests

from scale_build.exceptions import CallError
from scale_build.product_profiles import github_asset_request


URL = 'https://github.com/org/repo/releases/download/v1.1.0+nexus.amd64.1/pkg.deb'
ASSET = 'https://api.github.com/repos/org/repo/releases/assets/42'
TAG = 'v1.1.0+nexus.amd64.1'


def response(status, body, link=''):
    result = Mock(status_code=status, headers={'Link': link})
    result.json.return_value = body
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(str(status))
    return result


class DraftAssetTests(unittest.TestCase):
    def test_private_draft_fallback_paginates_official_release_lists_and_keeps_token_on_api(self):
        missing = response(404, {})
        first = response(200, [{'tag_name': 'other-' + str(index)} for index in range(100)])
        second = response(200, [{'tag_name': TAG, 'draft': True, 'id': 123,
                                 'assets': [{'name': 'pkg.deb', 'url': ASSET}]}])
        get = Mock(side_effect=[missing, first, second])
        with patch.dict(os.environ, {'HARBOROS_COMPONENT_READ_TOKEN': 'memory-only-fixture'}):
            url, headers = github_asset_request(URL, get)
        self.assertEqual(url, ASSET)
        self.assertEqual(headers['Authorization'], 'Bearer memory-only-fixture')
        self.assertEqual(get.call_args_list[1].args[0],
                         'https://api.github.com/repos/org/repo/releases?per_page=100&page=1')
        self.assertEqual(get.call_args_list[2].args[0],
                         'https://api.github.com/repos/org/repo/releases?per_page=100&page=2')
        for call in get.call_args_list:
            self.assertTrue(call.args[0].startswith('https://api.github.com/repos/org/repo/'))
        missing.raise_for_status.assert_not_called()

    def test_next_link_can_continue_a_short_page_without_following_its_url(self):
        get = Mock(side_effect=[response(404, {}),
                                response(200, [{'tag_name': 'other'}], '<https://untrusted.invalid>; rel="next"'),
                                response(200, [{'tag_name': TAG, 'assets': [{'name': 'pkg.deb', 'url': ASSET}]}])])
        self.assertEqual(github_asset_request(URL, get)[0], ASSET)
        self.assertTrue(get.call_args_list[2].args[0].startswith('https://api.github.com/repos/org/repo/releases?'))

    def test_authentication_and_server_errors_do_not_fall_back(self):
        for status in (401, 403, 500):
            get = Mock(return_value=response(status, {}))
            with self.assertRaises(requests.HTTPError):
                github_asset_request(URL, get)
            get.assert_called_once()

    def test_end_of_list_missing_or_ambiguous_exact_tag_fails_closed(self):
        for body in ([], [{'tag_name': 'other'}], [{'tag_name': TAG}, {'tag_name': TAG}]):
            get = Mock(side_effect=[response(404, {}), response(200, body)])
            with self.assertRaises(CallError):
                github_asset_request(URL, get)

    def test_fallback_still_rejects_wrong_repository_and_non_numeric_asset_urls(self):
        for asset in ('https://api.github.com/repos/another/repo/releases/assets/42',
                      'https://api.github.com/repos/org/repo/releases/assets/42?token=bad',
                      'https://attacker.invalid/42'):
            get = Mock(side_effect=[response(404, {}), response(200, [{'tag_name': TAG,
                       'assets': [{'name': 'pkg.deb', 'url': asset}]}])])
            with self.assertRaises(CallError):
                github_asset_request(URL, get)


if __name__ == '__main__':
    unittest.main()
