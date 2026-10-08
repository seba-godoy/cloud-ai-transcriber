import pathlib
import unittest


class Phase12DockerEntrypointTests(unittest.TestCase):
    def test_docker_uses_phase12_entrypoint(self):
        dockerfile = pathlib.Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn('CMD ["python", "phase12_entrypoint.py"]', dockerfile)

    def test_phase12_entrypoint_installs_runtime_before_main(self):
        entrypoint = pathlib.Path("phase12_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("install_phase12_runtime(app)", entrypoint)
        self.assertIn("app.main()", entrypoint)
        self.assertLess(entrypoint.index("install_phase12_runtime(app)"), entrypoint.index("app.main()"))


if __name__ == "__main__":
    unittest.main()
