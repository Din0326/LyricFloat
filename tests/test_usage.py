"""The app's own CPU and memory for the tray menu (FR-27)."""

from lyricfloat.diagnostics.usage import Usage, UsageMeter, private_working_set


class Clock:
    def __init__(self):
        self.cpu = 0.0
        self.wall = 100.0

    def run(self, wall: float, cpu: float) -> None:
        self.wall += wall
        self.cpu += cpu


def meter(clock, cpus=4, memory=lambda: 80 * 1048576):
    return UsageMeter(cpu_time=lambda: clock.cpu, clock=lambda: clock.wall, cpus=cpus, memory=memory)


def test_cpu_is_a_share_of_the_whole_machine_since_the_last_reading():
    clock = Clock()
    m = meter(clock)
    assert m.read().cpu_pct is None  # one reading is no interval
    clock.run(wall=5, cpu=0.2)  # 0.2 s of CPU in 5 s on 4 cores
    assert m.read().cpu_pct == 1.0
    clock.run(wall=1, cpu=0.0)
    assert m.read().cpu_pct == 0.0


def test_no_time_passed_gives_no_cpu_reading():
    clock = Clock()
    m = meter(clock)
    m.read()
    assert m.read().cpu_pct is None


def test_memory_in_mb():
    assert meter(Clock()).read().memory_mb == 80
    assert meter(Clock(), memory=lambda: None).read().memory_mb is None


def test_label():
    assert Usage(0.4321, 86.4).label() == "CPU 0.4%・記憶體 86 MB"
    assert Usage(None, None).label() == "CPU —・記憶體 —"


def test_private_working_set_is_read_from_windows():
    bytes_ = private_working_set()
    assert bytes_ is not None and 10 * 1048576 < bytes_ < 4 * 2**30  # this test process
