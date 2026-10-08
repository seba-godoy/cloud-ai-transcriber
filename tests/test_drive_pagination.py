import unittest
from unittest.mock import patch, MagicMock
from config import Config
from drive_watcher import list_pending_audio_files


class TestDrivePagination(unittest.TestCase):
    @patch("drive_watcher.get_drive_service")
    def test_pagination_accumulation(self, mock_get_service):
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_list = MagicMock()

        page1 = {
            "nextPageToken": "token_page_2",
            "files": [
                {"id": "id1", "name": "audio1.mp3", "mimeType": "audio/mpeg"},
                {"id": "id2", "name": "audio2.m4a", "mimeType": "audio/mp4"}
            ]
        }
        page2 = {
            "nextPageToken": None,
            "files": [
                {"id": "id3", "name": "video3.mp4", "mimeType": "video/mp4"}
            ]
        }

        mock_list.execute.side_effect = [page1, page2]
        mock_files.list.return_value = mock_list
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        mock_state_store = MagicMock()
        mock_state_store.is_processed.return_value = False
        mock_state_store.is_permanently_failed.return_value = False
        mock_state_store.is_retry_due.return_value = True
        mock_state_store._load.return_value = {}

        # Pagination is independent of environment bootstrapping. Make the
        # single Primary route explicit and disable the optional Secondary route so this
        # legacy test continues to assert exactly two provider calls/pages.
        with (
            patch.object(Config, "DRIVE_PRIMARY_INPUT_FOLDER_ID", "primary-input"),
            patch.object(Config, "DRIVE_PRIMARY_OUTPUT_FOLDER_ID", "primary-output"),
            patch.object(Config, "DRIVE_INPUT_FOLDER_ID", "primary-input"),
            patch.object(Config, "DRIVE_OUTPUT_FOLDER_ID", "primary-output"),
            patch.object(Config, "DRIVE_SECONDARY_INPUT_FOLDER_ID", None),
            patch.object(Config, "DRIVE_SECONDARY_OUTPUT_FOLDER_ID", None),
        ):
            result = list_pending_audio_files(mock_state_store)

        self.assertEqual(len(result), 3)
        item_ids = [item["id"] for item in result]
        self.assertEqual(item_ids, ["id1", "id2", "id3"])

        # Verify pageToken passed in second call
        self.assertEqual(mock_files.list.call_count, 2)
        second_call_kwargs = mock_files.list.call_args_list[1][1]
        self.assertEqual(second_call_kwargs.get("pageToken"), "token_page_2")


if __name__ == "__main__":
    unittest.main()
