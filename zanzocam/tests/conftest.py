import os
import json
import shutil
import pytest
import logging

from textwrap import dedent
from fractions import Fraction
from PIL import Image, ImageChops
from collections import namedtuple
from collections import defaultdict
from pathlib import Path, PosixPath

import webcam
import constants
from webcam.utils import log, log_error


def point_const_to_tmpdir(modules, monkeypatch, tmpdir):
    """
        Mocks all the calues in constants.py to point to the 
        pytest temp directory.
    """
    os.mkdir(tmpdir / "data")
    os.mkdir(tmpdir / "web_ui")
    os.mkdir(tmpdir / "data" / "overlays")

    base_path = str(constants.BASE_PATH.absolute()).strip()
    test_path = str(tmpdir).strip()
    for const, value in vars(constants).items():
        if (not const.startswith("_") and
                (isinstance(value, str) or 
                isinstance(value, PosixPath)
            )):
            new_value = str(value).replace(base_path, test_path)

            if isinstance(value, PosixPath):
                new_value = Path(new_value)

            monkeypatch.setattr(constants, const, new_value)
            for module in modules:
                monkeypatch.setattr(module, const, new_value)
            

class MockSystem:
    
    @staticmethod
    def report_general_status():
        return {'test-message':'good'}

    @staticmethod
    def apply_system_settings(settings):
        log("[TEST] applying system settings - mocked")
        return


class MockServer:
    def __init__(self, *a, **k):
        log("[TEST] init Server - mocked")

    def __getattr__(self, *a, **k):
        return

    def get_endpoint(self, *a, **k):
        return "[MOCKED TEST ENDPOINT]"

    def download_overlay_images(self, *a, **k):
        log("[TEST] downloading overlays images - mocked")
    
    def upload_logs(self, *a, **k):
        log("[TEST] uploading logs - mocked")

    def upload_diagnostics(self, *a, **k):
        log("[TEST] uploading diagnostics - mocked")
    
    def upload_failure_report(self, *a, **k):
        log("[TEST] uploading failure report - mocked")

    def upload_picture(self, *a, **k):
        log("[TEST] uploading picture - mocked")

    def update_configuration(self, *a, **k):
        return webcam.configuration.Configuration.create_from_dictionary({
            "server": {"new-test-config": "present"}
        })


class MockCamera:
    def __init__(self, config, *a, **k):
        log("[TEST] init Camera - mocked")
        if isinstance(config, webcam.configuration.Configuration):
            self.fail = bool(getattr(config, 'camera_will_fail', False))

    def __getattr__(self, *a, **k):
        return None

    def take_picture(self, *a, **k):
        log("[TEST] taking picture - mocked")

    def cleanup_image_files(self, *a, **k):
        log("[TEST] cleanup image files - mocked")


class MockConfig:
    def __init__(self, *a, **k):
        log("[TEST] init Config - mocked")

    def __getattr__(self, *a, **k):
        return lambda *a, **k: None

    def within_active_hours(self):
        return True
    
    def __str__(self):
        return json.dumps(vars(self), indent=4, default=lambda x: str(x))


@pytest.fixture(autouse=True)
def point_to_tmpdir(monkeypatch, tmpdir):
    modules = [
        webcam.main,
        webcam.system,
        webcam.server.server,
        webcam.server.http_server,
        webcam.server.ftp_server,
        webcam.camera,
        webcam.overlays,
        webcam.configuration
    ]
    point_const_to_tmpdir(modules, monkeypatch, tmpdir)
    webcam.system.CRONJOB_FILE = tmpdir / "zanzocam"


@pytest.fixture()
def mock_modules(monkeypatch, tmpdir):
    """
        Used in tests of main.py to mock all submodules
    """
    monkeypatch.setattr(webcam.main, 'System', MockSystem)
    monkeypatch.setattr(webcam.main, 'Server', MockServer)    
    monkeypatch.setattr(webcam.main, 'Camera', MockCamera)
    monkeypatch.setattr(webcam.main, 'Configuration', MockConfig)
    monkeypatch.setattr(webcam.main, 'WAIT_AFTER_CAMERA_FAIL', 1)


@pytest.fixture()
def mock_modules_apart_config(monkeypatch):
    """
        Used in tests of main.py to mock all submodules,
        apart from Configuration, which is very simple and
        deeply used
    """
    monkeypatch.setattr(webcam.main, 'System', MockSystem)
    monkeypatch.setattr(webcam.main, 'Server', MockServer)    
    monkeypatch.setattr(webcam.main, 'Camera', MockCamera)
    monkeypatch.setattr(webcam.main, 'WAIT_AFTER_CAMERA_FAIL', 1)


class MockFTP:
    def __init__(self, *a, **k):
        pass
    def prot_p(self, *a, **k):
        pass
    def cwd(self, folder, **k):
        pass
    def retrbinary(self, bin_to_download, callback, **k):

        # If you're asking for the config file, download it
        if str(constants.CONFIGURATION_FILE.name) in bin_to_download:
            for line in open(constants.CONFIGURATION_FILE, 'r').readlines():
                callback(line.encode(constants.FTP_CONFIG_FILE_ENCODING))
            return "226 OK"

        # Else, download an overlay
        else:
            image = Image.new("RGB", (10, 10), color="#FFFFFF")
            image.save(str(constants.BASE_PATH /'test.png'))
            with open(constants.BASE_PATH /'test.png', 'rb') as o:
                callback(o.read())
            return "226 OK"

    def storlines(self, command, lines, **k):
        with open(constants.BASE_PATH / "test_received_logs.txt", 'wb') as r:
            r.writelines(lines)
        return "226 OK"

    def storbinary(self, command, file_handle, **k):
        name = command[14:]
        with open(constants.BASE_PATH / ("r_"+name), 'wb') as r:
            r.write(file_handle.read())
        return "226 OK"

    def rename(self, old, new, **k):
        return "226 OK"


@pytest.fixture(autouse=True)
def mock_ftplib(monkeypatch):
    monkeypatch.setattr(webcam.server.ftp_server, "FTP", MockFTP)
    monkeypatch.setattr(webcam.server.ftp_server, "FTP_TLS", MockFTP)
    monkeypatch.setattr(webcam.server.ftp_server, "_Patched_FTP_TLS", MockFTP)


class MockCredentials:
    def __init__(self, u, p):
        pass

class MockGetRequest:

    def __init__(self, data=None, status=200, file_stream=None):
        self.data = data
        self.raw = file_stream
        self.status_code = status
        self.reason = "TEST REASON"

    def json(self):
        if self.data:
            return json.loads(self.data)
        return {}

class MockPostRequest:

    def __init__(self, data=None, image=None, response=None, status=200, tmpdir=None):
        if data:
            webcam.utils.log(f"[TEST] POSTing: {data}")

        if image and tmpdir:
            webcam.utils.log(f"[TEST] POSTing an image")
            with open(tmpdir / "received_image.jpg", "wb") as received:
                received.write(image['photo'].read())

        if response:
            self.data = response
        else:
            self.data = json.dumps({
                "logs": "",
                "photo": "",
            })
        self.status_code = status
        self.reason = "TEST REASON"

    def json(self):
        if self.data:
            return json.loads(self.data)
        return {}


@pytest.fixture(autouse=True)
def mock_requests(monkeypatch):
    monkeypatch.setattr(webcam.server.http_server.requests.auth,
                        'HTTPBasicAuth',
                        lambda u, p: MockCredentials(u, p))

    def default_get_behavior(url, auth=None, timeout=None, *a, **k):
        
        # If you're asking for the config file, download it
        if not any(ext in url.lower() for ext in ['.jpeg', '.png', '.gif']):
            return MockGetRequest(data=
                '{"configuration": \n' +
                open(constants.CONFIGURATION_FILE, 'r').read() +
                '\n}'
            )
        
        # Else, download an overlay
        else:
            image = Image.new("RGB", (10, 10), color="#FFFFFF")
            image.save(str(constants.BASE_PATH /'test.png'))
            return MockGetRequest(file_stream=open(constants.BASE_PATH/'test.png', 'rb'))
    
    monkeypatch.setattr(webcam.server.http_server.requests, 
                        'get', default_get_behavior)

    monkeypatch.setattr(webcam.server.http_server.requests,
                        'post', lambda *a, **k: MockPostRequest())


MockResolution = namedtuple('PiResolution', 'width height')
MockFramerateRange = namedtuple('PiFramerateRange', 'low high')


class MockPiCamera:
    def __init__(self, sensor_mode=None, framerate_range=None, *a, **k):
        self.sensor_mode = sensor_mode
        if framerate_range:
            self.framerate_range = MockFramerateRange(*framerate_range)
        else:
            self.framerate_range = MockFramerateRange(Fraction(30, 1), Fraction(30, 1))
        self.MAX_RESOLUTION = MockResolution(10000, 10000)

    # For the with statement
    def __enter__(self):
        return self

    # For the with statement
    def __exit__(self, *a, **k):
        return

    def __getattr__(self, *a, **k):
        return

    def capture(self, path, *a, **k):
        Image.new("RGB", (64, 48), color="#FF0000").save(path)


@pytest.fixture(autouse=True)
def mock_piexif(monkeypatch):
    """
        Used in the tests of camera.py to mock away PIEXIF
    """
    monkeypatch.setattr(
        webcam.camera.piexif,
        'load',
        lambda *a, **k: defaultdict(lambda: defaultdict(lambda: ""))
    )
    monkeypatch.setattr(
        webcam.camera.piexif,
        'dump',
        lambda *a, **k: None
    )
    monkeypatch.setattr(webcam.camera.piexif.ImageIFD, 'Make', None)
    monkeypatch.setattr(webcam.camera.piexif.ImageIFD, 'Software', None)
    monkeypatch.setattr(webcam.camera.piexif.ImageIFD, 'ProcessingSoftware', None)


@pytest.fixture
def meminfo():
    yield dedent("""\n
        MemTotal:         245724 kB
        MemFree:          146968 kB
        MemAvailable:     160988 kB
        Buffers:           20764 kB
        Cached:            38232 kB
        SwapCached:         1024 kB
        Active:            57428 kB
        Inactive:          12616 kB
        Active(anon):       9140 kB
        Inactive(anon):     6372 kB
        Active(file):      48288 kB
        Inactive(file):     6244 kB
        Unevictable:          16 kB
        Mlocked:              16 kB
        SwapTotal:        102396 kB
        SwapFree:          61948 kB
        Dirty:                 0 kB
        Writeback:             0 kB
        AnonPages:         10628 kB
        Mapped:            13660 kB
        Shmem:              4464 kB
        KReclaimable:       9084 kB
        Slab:              18772 kB
        SReclaimable:       9084 kB
        SUnreclaim:         9688 kB
        KernelStack:         832 kB
        PageTables:         1732 kB
        NFS_Unstable:          0 kB
        Bounce:                0 kB
        WritebackTmp:          0 kB
        CommitLimit:      225256 kB
        Committed_AS:     348148 kB
        VmallocTotal:     770048 kB
        VmallocUsed:        3352 kB
        VmallocChunk:          0 kB
        Percpu:               64 kB
        CmaTotal:          65536 kB
        CmaFree:           58580 kB
    """)


@pytest.fixture
def logs(monkeypatch):
    logs = []

    def mock_log(msg, *args, **kwargs):
        print(msg)
        logs.append(msg)
    
    logging.info = mock_log
    yield logs
    logs = []


def in_logs(logs, string):
    """
        Looks for a string in the entire logs stack
    """
    total = "\n".join(logs)
    try:
        where = total.index(string) + 1  # So that 0 == not found
        print(f"---------> {string}: {where}")
        return True
    except ValueError:
        return False