import gevent
import gevent.monkey

gevent.monkey.patch_all()
import psycogreen.gevent

psycogreen.gevent.patch_psycopg()
import psycopg2
from datetime import datetime, timezone
from locust import events
import socket
import greenlet
import logging
import atexit
import os
import sys
from dateutil import parser

GRAFANA_URL = os.environ["LOCUST_GRAFANA_URL"]


class Reporter:  # pylint: disable=R0902
    """
    Reporter logs locust samples/events to a Postgres Timescale database.

    It assumes you have a table like this set up:
    CREATE TABLE public.request
    (
        "time" timestamp with time zone NOT NULL,
        run_id timestamp with time zone NOT NULL,
        exception text COLLATE pg_catalog."default",
        greenlet_id integer NOT NULL,
        loadgen text COLLATE pg_catalog."default" NOT NULL,
        name text COLLATE pg_catalog."default" NOT NULL,
        request_type text COLLATE pg_catalog."default" NOT NULL,
        response_length integer,
        response_time double precision,
        success smallint NOT NULL,
        testplan text COLLATE pg_catalog."default"
    )
    WITH (
        OIDS = FALSE
    )
    CREATE INDEX request_time_idx
        ON public.request USING btree
        ("time" DESC)
        TABLESPACE pg_default;
    CREATE TRIGGER ts_insert_blocker
        BEFORE INSERT
        ON public.request
        FOR EACH ROW
        EXECUTE PROCEDURE _timescaledb_internal.insert_blocker();
    """

    def __init__(self, testplan, profile_name="", description=""):
        try:
            self._conn = psycopg2.connect(host=os.environ["PGHOST"])
        except Exception:
            logging.error(
                "Use standard postgres env vars to specify where to report locust samples (https://www.postgresql.org/docs/11/libpq-envars.html)"
            )
            raise
        self._conn.autocommit = True
        self._cur = self._conn.cursor()
        assert testplan != ""
        self._testplan = testplan
        self._hostname = socket.gethostname()
        self._samples = []
        self._finished = False
        self._background = gevent.spawn(self._run)
        if is_slave() or is_master():
            # swarm generates the run id for its master and slaves
            self._run_id = parser.parse(os.environ["LOCUST_RUN_ID"])
        else:
            # non-swarm runs need to generate the run id here
            self._run_id = datetime.now(timezone.utc)
        self._profile_name = profile_name
        self._rps = os.getenv("LOCUST_RPS", "0")
        self._description = description

        if not is_slave():
            self.log_start_testrun()

        events.request_success += self.request_success
        events.request_failure += self.request_failure
        events.quitting += self.quitting
        atexit.register(self.exit)

    def _run(self):
        while True:
            if self._samples:
                # Buffer samples, so that a locust greenlet will write to the new list
                # instead of the one that has been sent into postgres client
                samples_buffer = self._samples
                self._samples = []
                self.write_samples_to_db(samples_buffer)
            else:
                if self._finished:
                    break
            gevent.sleep(0.5)

    def write_samples_to_db(self, samples):
        try:
            self._cur.executemany(
                """INSERT INTO request(time,run_id,greenlet_id,loadgen,name,request_type,response_time,success,testplan,response_length,exception) VALUES
 (%(time)s, %(run_id)s, %(greenlet_id)s, %(loadgen)s, %(name)s, %(request_type)s, %(response_time)s, %(success)s, %(testplan)s, %(response_length)s, %(exception)s)""",
                samples,
            )
        except psycopg2.Error as error:
            logging.error("Failed to write samples to Postgresql timescale database: " + repr(error))

    def quitting(self):
        self._finished = True
        atexit._clear()  # make sure we dont capture additional ctrl-c:s
        self._background.join()
        self.exit()

    def _log_request(self, request_type, name, response_time, response_length, success, exception):
        current_greenlet = greenlet.getcurrent()  # pylint: disable=I1101
        if hasattr(current_greenlet, "minimal_ident"):
            greenlet_id = current_greenlet.minimal_ident
        else:
            greenlet_id = -1  # if no greenlet has been spawned (typically when debugging)

        sample = {
            "time": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "greenlet_id": greenlet_id,
            "loadgen": self._hostname,
            "name": name,
            "request_type": request_type,
            "response_time": response_time,
            "success": success,
            "testplan": self._testplan,
        }

        if response_length >= 0:
            sample["response_length"] = response_length
        else:
            sample["response_length"] = None

        if exception:
            sample["exception"] = repr(exception)
        else:
            sample["exception"] = None

        self._samples.append(sample)

    def request_success(self, request_type, name, response_time, response_length):
        self._log_request(request_type, name, response_time, response_length, 1, None)

    def request_failure(self, request_type, name, response_time, exception):
        self._log_request(request_type, name, response_time, -1, 0, exception)

    def log_start_testrun(self):
        num_clients = 1
        for index, arg in enumerate(sys.argv):
            if arg == "-c":
                num_clients = sys.argv[index + 1]

        self._cur.execute(
            "INSERT INTO testrun (id, testplan, profile_name, num_clients, rps, description) VALUES (%s,%s,%s,%s,%s,%s)",
            (self._run_id, self._testplan, self._profile_name, num_clients, self._rps, self._description),
        )

        self._cur.execute(
            "INSERT INTO events (time, text) VALUES (%s, %s)",
            (datetime.now(timezone.utc).isoformat(), self._testplan + " started"),
        )

    def log_stop_test_run(self):
        end_time = datetime.now(timezone.utc)
        try:
            self._cur.execute("UPDATE testrun SET end_time = %s where id = %s", (end_time, self._run_id))
            self._cur.execute(
                "INSERT INTO events (time, text) VALUES (%s, %s)", (end_time, self._testplan + " finished")
            )
        except psycopg2.Error as error:
            logging.error(
                "Failed to update testrun record (or events) with end time to Postgresql timescale database: "
                + repr(error)
            )
        logging.info(
            f"Report: {GRAFANA_URL}&var-testplan={self._testplan}&from={int(self._run_id.timestamp()*1000)}&to={int((end_time.timestamp()+1)*1000)}\n"
        )

    def exit(self):
        if not is_slave():  # on master or standalone locust run
            self.log_stop_test_run()
        if self._conn:
            self._cur.close()
            self._conn.close()


def is_slave():
    return "--slave" in sys.argv


def is_master():
    return "--master" in sys.argv
