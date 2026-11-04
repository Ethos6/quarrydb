import os
import subprocess
import sys
import tempfile
import unittest

from quarrydb.cli import render_table


class CLITests(unittest.TestCase):
    def run_shell(self, script, *args):
        env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), ".."))
        return subprocess.run([sys.executable, "-m", "quarrydb", *args], input=script, capture_output=True,
                              text=True, env=env, timeout=60)

    def test_script_through_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cli.qdb")
            r = self.run_shell("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);\n"
                               "INSERT INTO t (v) VALUES ('a'),\n ('b');\nSELECT * FROM t;\n.tables\n.check\n", path)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("| id | v |", r.stdout)
            self.assertIn("2 row(s)", r.stdout)
            self.assertIn("\nt\n", r.stdout)
            self.assertIn("ok", r.stdout)
            r = self.run_shell("", path, "-c", "SELECT COUNT(*) AS n FROM t", "--mode", "csv")
            self.assertEqual(r.stdout.strip().splitlines(), ["n", "2"])

    def test_error_does_not_abort(self):
        r = self.run_shell("SELECT * FROM missing;\nSELECT 1 AS one;\n")
        self.assertIn("Error: no such table: missing", r.stdout)
        self.assertIn("one", r.stdout)

    def test_render_table(self):
        out = render_table(["id", "name"], [(1, "ada"), (None, b"\x01")])
        self.assertIn("| NULL | X'01' |", out)
        self.assertIn("|    1 | ada   |", out)


if __name__ == "__main__":
    unittest.main()
