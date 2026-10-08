import subprocess
import unittest
from unittest.mock import patch

import gemini_transcriber as gt


class MediaDurationProbeTests(unittest.TestCase):
    def completed(self, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(
            args=["tool"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    def called_error(self, stderr):
        return subprocess.CalledProcessError(
            1, ["tool"], output="", stderr=stderr
        )

    def test_container_duration_is_fast_path(self):
        with patch.object(
            gt.subprocess,
            "run",
            return_value=self.completed(stdout="123.456\n"),
        ) as run:
            duration = gt.probe_audio_duration("/tmp/audio.m4a")

        self.assertAlmostEqual(duration, 123.456)
        self.assertEqual(run.call_count, 1)

    def test_audio_stream_duration_recovers_failed_container_probe(self):
        with patch.object(
            gt.subprocess,
            "run",
            side_effect=[
                self.called_error("[mov,mp4,m4a] container duration unavailable"),
                self.completed(stdout="77.25\n"),
            ],
        ):
            duration = gt.probe_audio_duration("/tmp/audio.m4a")

        self.assertAlmostEqual(duration, 77.25)

    def test_decode_progress_recovers_when_both_ffprobe_probes_fail(self):
        progress = (
            "frame=0\n"
            "out_time=00:00:30.000000\n"
            "progress=continue\n"
            "out_time=00:02:03.500000\n"
            "progress=end\n"
        )
        with patch.object(
            gt.subprocess,
            "run",
            side_effect=[
                self.called_error("container probe failed"),
                self.called_error("stream probe failed"),
                self.completed(stdout=progress),
            ],
        ) as run:
            duration = gt.probe_audio_duration("/tmp/audio.m4a")

        self.assertAlmostEqual(duration, 123.5)
        self.assertEqual(run.call_count, 3)
        decode_kwargs = run.call_args_list[2].kwargs
        self.assertEqual(
            decode_kwargs["timeout"],
            gt.DURATION_FALLBACK_TIMEOUT_SECONDS,
        )

    def test_failure_surfaces_real_ffmpeg_stderr(self):
        with patch.object(
            gt.subprocess,
            "run",
            side_effect=[
                self.called_error("moov atom not found"),
                self.called_error("invalid data found when processing input"),
                self.called_error("decoder could not open input"),
            ],
        ):
            with self.assertRaises(ValueError) as caught:
                gt.probe_audio_duration("/tmp/audio.m4a")

        message = str(caught.exception)
        self.assertIn("moov atom not found", message)
        self.assertIn("invalid data found when processing input", message)
        self.assertIn("decoder could not open input", message)

    def test_progress_parser_uses_greatest_valid_timestamp(self):
        self.assertEqual(
            gt._ffmpeg_progress_duration(
                "out_time=N/A\nout_time=00:00:01.250000\nout_time=00:00:03.750000\n"
            ),
            3.75,
        )


if __name__ == "__main__":
    unittest.main()
