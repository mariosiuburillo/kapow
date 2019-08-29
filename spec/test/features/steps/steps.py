from contextlib import suppress
from time import sleep
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import requests
from environconfig import EnvironConfig, StringVar, IntVar, BooleanVar
from comparedict import is_subset
import jsonexample

import logging


WORD2POS = {"first": 0, "second": 1, "last": -1}
HERE = os.path.dirname(__file__)


class Env(EnvironConfig):
    #: How to run Kapow! server
    KAPOW_SERVER_CMD = StringVar(default="kapow server")

    #: Where the Control API is
    KAPOW_CONTROLAPI_URL = StringVar(default="http://localhost:8081")

    #: Where the Data API is
    KAPOW_DATAAPI_URL = StringVar(default="http://localhost:8080")

    #: Where the User Interface is
    KAPOW_USER_URL = StringVar(default="http://localhost:8080")

    KAPOW_BOOT_TIMEOUT = IntVar(default=10)

    KAPOW_DEBUG_TESTS = BooleanVar(default=False)


if Env.KAPOW_DEBUG_TESTS:
    # These two lines enable debugging at httplib level
    # (requests->urllib3->http.client) You will see the REQUEST,
    # including HEADERS and DATA, and RESPONSE with HEADERS but without
    # DATA.  The only thing missing will be the response.body which is
    # not logged.
    try:
        import http.client as http_client
    except ImportError:
        # Python 2
        import httplib as http_client
    http_client.HTTPConnection.debuglevel = 1

    # You must initialize logging, otherwise you'll not see debug output.
    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True

def run_kapow_server(context):
    context.server = subprocess.Popen(
        shlex.split(Env.KAPOW_SERVER_CMD),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False)

    # Check process is running with reachable APIs
    open_ports = False
    for _ in range(Env.KAPOW_BOOT_TIMEOUT):
        is_running = context.server.poll() is None
        assert is_running, "Server is not running!"
        with suppress(requests.exceptions.ConnectionError):
            open_ports = (
                requests.head(Env.KAPOW_CONTROLAPI_URL, timeout=1).status_code
                and requests.head(Env.KAPOW_DATAAPI_URL, timeout=1).status_code)
            if open_ports:
                break
        sleep(1)

    assert open_ports, "API is unreachable after KAPOW_BOOT_TIMEOUT"

@given('I have a just started Kapow! server')
@given('I have a running Kapow! server')
def step_impl(context):
    run_kapow_server(context)


@when('I request a routes listing')
def step_impl(context):
    context.response = requests.get(f"{Env.KAPOW_CONTROLAPI_URL}/routes")


@given('I have a Kapow! server with the following routes')
def step_impl(context):
    run_kapow_server(context)

    if not hasattr(context, 'table'):
        raise RuntimeError("A table must be set for this step.")

    for row in context.table:
        response = requests.post(f"{Env.KAPOW_CONTROLAPI_URL}/routes",
                                 json={h: row[h] for h in row.headings})
        response.raise_for_status()


@given('I have a Kapow! server with the following testing routes')
def step_impl(context):
    run_kapow_server(context)

    if not hasattr(context, 'table'):
        raise RuntimeError("A table must be set for this step.")

    for row in context.table:
        response = requests.post(
            f"{Env.KAPOW_CONTROLAPI_URL}/routes",
            json={"entrypoint": " ".join(
                      [sys.executable,
                       shlex.quote(os.path.join(HERE, "testinghandler.py")),
                       shlex.quote(context.handler_fifo_path)]),  # Created in before_scenario
                  **{h: row[h] for h in row.headings}})
        response.raise_for_status()


@when('I send a request to the testing route "{path}"')
def step_impl(context, path):
    # Run the request in background
    def _testing_request():
        context.testing_response = requests.get(f"{Env.KAPOW_USER_URL}{path}")
    context.testing_request = threading.Thread(target=_testing_request)
    context.testing_request.start()

    # Block until the handler connects and give us its pid and the
    # handler_id
    with open(context.handler_fifo_path, 'r') as fifo:
        (context.testing_handler_pid,
         context.testing_handler_id) = fifo.readline().split(';')


@when('I release the testing request')
def step_impl(context):
    os.kill(int(context.testing_handler_pid), signal.SIGTERM)
    context.testing_request.join()


@when('I append the route')
def step_impl(context):
    context.response = requests.post(f"{Env.KAPOW_CONTROLAPI_URL}/routes",
                                     data=context.text,
                                     headers={"Content-Type": "application/json"})


@then('I get {code} as response code')
def step_impl(context, code):
    assert context.response.status_code == int(code), f"Got {context.response.status_code} instead"


@then('I get "{reason}" as response reason phrase')
def step_impl(context, reason):
    assert context.response.reason == reason, f"Got {context.response.reason} instead"


@then('I get the following response body')
def step_impl(context):
    assert is_subset(jsonexample.loads(context.text), context.response.json())


@when('I delete the route with id "{id}"')
def step_impl(context, id):
    context.response = requests.delete(f"{Env.KAPOW_CONTROLAPI_URL}/routes/{id}")


@given('I insert the route')
@when('I insert the route')
def step_impl(context):
    context.response = requests.put(f"{Env.KAPOW_CONTROLAPI_URL}/routes",
                                    headers={"Content-Type": "application/json"},
                                    data=context.text)


@when('I try to append with this malformed JSON document')
def step_impl(context):
    context.response = requests.post(
        f"{Env.KAPOW_CONTROLAPI_URL}/routes",
        headers={"Content-Type": "application/json"},
        data=context.text)


@when('I delete the {order} route')
def step_impl(context, order):
    idx = WORD2POS.get(order)
    routes = requests.get(f"{Env.KAPOW_CONTROLAPI_URL}/routes")
    id = routes.json()[idx]["id"]
    context.response = requests.delete(f"{Env.KAPOW_CONTROLAPI_URL}/routes/{id}")


@when('I try to insert with this JSON document')
def step_impl(context):
    context.response = requests.put(
        f"{Env.KAPOW_CONTROLAPI_URL}/routes",
        headers={"Content-Type": "application/json"},
        data=context.text)

@when('I get the route with id "{id}"')
def step_impl(context, id):
    context.response = requests.get(f"{Env.KAPOW_CONTROLAPI_URL}/routes/{id}")


@when('I get the {order} route')
def step_impl(context, order):
    idx = WORD2POS.get(order)
    routes = requests.get(f"{Env.KAPOW_CONTROLAPI_URL}/routes")
    id = routes.json()[idx]["id"]
    context.response = requests.get(f"{Env.KAPOW_CONTROLAPI_URL}/routes/{id}")


@when('I send a background request to the route "{route}"')
def step_imp(context, route):
    def _back():
        resource_route = f"{Env.KAPOW_DATAAPI_URL}/{route}"
        return requests.get(resource_route)
    context.testing_request = threading.Thread(target=_back)
    context.testing_request.start()


@when('I get the resource "{resource}" for the current request handler')
def step_imp(context, resource):
    def retrieve_request_id():
        requests_dir = os.path.exists('/tmp/wip')
        while not requests_dir:
            time.sleep(1)
            requests_dir = os.path.exists('/tmp/wip')
        target_count = len(os.listdir('/tmp/wip'))
        while target_count <= 0:
            time.sleep(1)
            target_count = len(os.listdir('/tmp/wip'))
        target = os.listdir('/tmp/wip')[0]

        with open(os.path.join("/tmp/wip", target), "r") as f:
            return f.readline().strip()
    def remove_request_id(request_id):
        os.remove(os.path.join("/tmp/wip", request_id))
    background_request_id = retrieve_request_id()
    resource = f"{Env.KAPOW_CONTROLAPI_URL}/handlers/{background_request_id}/{resource}"
    context.response = requests.get(resource)
    remove_request_id(background_request_id)


@then('I get the following raw body')
def step_impl(context):
    assert is_subset(context.text.strip(), context.response.text.strip()) 
