import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
os.environ["TESSERA_SIM_BASE"] = tempfile.mkdtemp(prefix="tessera-sim-test-")
sys.path.insert(0, str(APP_DIR))

from fastapi.testclient import TestClient  # noqa: E402
import tessera_sim  # noqa: E402
import topology_monitor  # noqa: E402
import log_store  # noqa: E402
from log_store import list_logs, record_log, set_processor_ignored, set_processor_paused, parse_syslog_message  # noqa: E402


class TesseraSimulatorSmokeTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(tessera_sim.app)
        log_store.init_log_db()
        with log_store.connect() as conn:
            conn.execute("DELETE FROM processor_logs")
            conn.execute("DELETE FROM paused_log_buffer")
            conn.execute("DELETE FROM processors")

    def test_home_page_links_to_main_tools(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Tessera Control and Monitoring", response.text)
        self.assertIn('href="/api-contents"', response.text)
        self.assertIn('href="/god"', response.text)
        self.assertIn('href="/logs"', response.text)
        self.assertIn('href="/topology"', response.text)

    def test_api_contents_page_shows_current_state(self):
        response = self.client.get("/api-contents")

        self.assertEqual(response.status_code, 200)
        self.assertIn("/api/system/processor-type", response.text)
        self.assertIn("sx40", response.text)

    def test_processor_logs_page_loads_without_logs(self):
        response = self.client.get("/logs")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Processor Logs", response.text)
        self.assertIn("No logs received yet.", response.text)

    def test_processor_log_management_page_loads(self):
        response = self.client.get("/logs/manage")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Manage Processor Logs", response.text)
        self.assertIn("Storage used by logs", response.text)

    def test_processor_logs_export_returns_csv(self):
        response = self.client.get("/logs/export?minutes=60")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        self.assertIn("received_at_utc,processor_time,processor_name,processor_ip,type", response.text)

    def test_syslog_message_parser_strips_processor_time_and_type(self):
        priority, facility, severity, processor_time, message_type, message = parse_syslog_message(
            "<13>Jun 12 21:59:46 tessera: Source Auth: requests authentication"
        )

        self.assertEqual(priority, 13)
        self.assertEqual(facility, 1)
        self.assertEqual(severity, 5)
        self.assertEqual(processor_time, "Jun 12 21:59:46")
        self.assertEqual(message_type, "tessera")
        self.assertEqual(message, "Source Auth: requests authentication")

    def test_paused_and_ignored_log_collection(self):
        record_log("192.0.2.50", "UDP", "<13>Jun 12 21:59:46 tessera: before pause")
        set_processor_paused("192.0.2.50", True)
        record_log("192.0.2.50", "UDP", "<13>Jun 12 21:59:47 tessera: while paused")
        self.assertEqual([row["message"] for row in list_logs("192.0.2.50")], ["before pause"])

        set_processor_paused("192.0.2.50", False)
        self.assertEqual([row["message"] for row in list_logs("192.0.2.50")], ["while paused", "before pause"])

        set_processor_ignored("192.0.2.50", True)
        record_log("192.0.2.50", "UDP", "<13>Jun 12 21:59:48 tessera: ignored")
        self.assertNotIn("ignored", [row["message"] for row in list_logs("192.0.2.50")])

    def test_topology_page_loads_without_monitors(self):
        response = self.client.get("/topology")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Topology Monitoring", response.text)
        self.assertIn("No processors are being monitored yet.", response.text)

    def test_topology_no_connection_pair_draws_red_x(self):
        loop_state = (
            "no-loop-found: A1->No connection, "
            "loop-found: A2->B2, "
            "no-loop-found: B1->No connection"
        )
        parsed = topology_monitor.parse_loop_state(loop_state)
        self.assertEqual(parsed[0]["end"], "NO CONNECTION")

        svg = topology_monitor.topology_svg({
            "id": "test",
            "name": "Test",
            "ip": "192.0.2.10",
            "processor_type": "sx40",
            "loop1_state": loop_state,
            "loop2_state": "",
        })

        self.assertEqual(svg.count('class="error-x"'), 1)
        self.assertEqual(svg.count('class="arrow bad"'), 2)
        self.assertEqual(svg.count('marker-start="url(#arrow-bad-test)"'), 2)

    def test_api_root_returns_default_tree(self):
        response = self.client.get("/api")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("api", body)
        self.assertEqual(body["api"]["system"]["processor-type"], "sx40")

    def test_read_write_endpoint_updates_state(self):
        response = self.client.put(
            "/api/output/global-colour/brightness",
            json={"data": 5000},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"brightness": 5000})
        readback = self.client.get("/api/output/global-colour/brightness")
        self.assertEqual(readback.json(), {"brightness": 5000})

    def test_read_only_endpoint_rejects_write(self):
        response = self.client.put(
            "/api/system/processor-type",
            json={"data": "s8"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"response-code": "Bad operation"})

    def test_out_of_range_endpoint_rejects_write(self):
        response = self.client.put(
            "/api/output/global-colour/brightness",
            json={"data": 10001},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"response-code": "Bad input parameter value"})


if __name__ == "__main__":
    unittest.main()
