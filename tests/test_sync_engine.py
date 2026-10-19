import pytest

from lyricfloat.models import PlaybackAnchor
from lyricfloat.sync.engine import SyncEngine

STARTS = [5.0, 10.0, 15.0, 20.0, 25.0]
DURATION = 30.0


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def engine(clock):
    e = SyncEngine(clock=clock)
    e.set_lines(STARTS)
    return e


def anchor(clock, position, playing=True, duration=DURATION):
    return PlaybackAnchor(position=position, at=clock(), playing=playing, duration=duration)


@pytest.mark.parametrize(
    "position, expected",
    [(0.0, -1), (4.99, -1), (5.0, 0), (7.5, 1 - 1), (10.0, 1), (14.999, 1), (25.0, 4), (29.0, 4)],
)
def test_binary_search_locates_line(engine, clock, position, expected):
    engine.update(anchor(clock, position))
    assert engine.index == expected


def test_no_anchor_means_no_line(clock):
    e = SyncEngine(clock=clock)
    assert e.set_lines(STARTS).index == -1
    assert e.position() is None
    assert e.seconds_to_next() is None


def test_position_advances_locally_while_playing(engine, clock):
    engine.update(anchor(clock, 8.0))
    clock.advance(1.5)
    assert engine.position() == pytest.approx(9.5)


def test_tick_moves_to_next_line_with_animation(engine, clock):
    engine.update(anchor(clock, 9.0))
    assert engine.tick() is None
    clock.advance(1.2)
    update = engine.tick()
    assert update.index == 1
    assert update.animate
    assert update.reason == "tick"


def test_seconds_to_next_points_at_next_boundary(engine, clock):
    engine.update(anchor(clock, 6.0))
    assert engine.seconds_to_next() == pytest.approx(4.0)
    engine.update(anchor(clock, 26.0))
    assert engine.seconds_to_next() is None  # last line


def test_pause_freezes_position_and_schedule(engine, clock):
    engine.update(anchor(clock, 12.0, playing=False))
    clock.advance(30)
    assert engine.position() == pytest.approx(12.0)
    assert engine.tick() is None
    assert engine.index == 1
    assert engine.seconds_to_next() is None


def test_resume_after_pause_is_not_a_seek(engine, clock):
    engine.update(anchor(clock, 12.0, playing=False))
    clock.advance(60)
    assert engine.update(anchor(clock, 12.05)) is None  # same line, no jump


def test_small_correction_is_absorbed(engine, clock):
    engine.update(anchor(clock, 11.0))
    clock.advance(1.0)
    assert engine.update(anchor(clock, 13.5)) is None  # predicted 12.0, 1.5 s drift < 2 s


def test_small_correction_across_boundary_animates(engine, clock):
    engine.update(anchor(clock, 13.0))
    clock.advance(0.5)
    update = engine.update(anchor(clock, 15.2))  # predicted 13.5, crosses into line 2
    assert update.index == 2
    assert update.animate
    assert update.reason == "anchor"


def test_jitter_just_after_boundary_does_not_step_back(engine, clock):
    engine.update(anchor(clock, 10.05))
    assert engine.index == 1
    assert engine.update(anchor(clock, 9.9)) is None
    assert engine.index == 1


def test_real_backward_correction_steps_back(engine, clock):
    engine.update(anchor(clock, 10.05))
    update = engine.update(anchor(clock, 9.0))  # 1.05 s back, beyond the jitter tolerance
    assert update.index == 0
    assert update.animate


def test_forward_seek_repositions_without_animation(engine, clock):
    engine.update(anchor(clock, 6.0))
    clock.advance(0.5)
    update = engine.update(anchor(clock, 21.0))
    assert update.index == 3
    assert not update.animate
    assert update.reason == "seek"


def test_backward_seek_repositions_without_animation(engine, clock):
    engine.update(anchor(clock, 22.0))
    update = engine.update(anchor(clock, 11.0))
    assert update.index == 1
    assert not update.animate
    assert update.reason == "seek"


def test_seek_within_same_line_still_reports(engine, clock):
    engine.update(anchor(clock, 15.5))
    update = engine.update(anchor(clock, 19.5))  # 4 s jump, same line 2
    assert update is not None
    assert update.index == 2
    assert update.reason == "seek"


def test_loop_from_end_to_start_resets_line(engine, clock):
    engine.update(anchor(clock, 28.0))
    clock.advance(2.0)
    update = engine.update(anchor(clock, 0.3))
    assert update.index == -1
    assert not update.animate
    assert update.reason == "loop"


def test_position_is_capped_at_duration(engine, clock):
    engine.update(anchor(clock, 29.0))
    clock.advance(10)
    assert engine.position() == pytest.approx(DURATION)


def test_lyrics_arriving_after_anchor_are_positioned_immediately(clock):
    e = SyncEngine(clock=clock)
    e.update(anchor(clock, 17.0))
    update = e.set_lines(STARTS)
    assert update.index == 2
    assert not update.animate


def test_clear_lines_keeps_anchor(engine, clock):
    engine.update(anchor(clock, 17.0))
    engine.clear_lines()
    assert engine.index == -1
    assert engine.position() == pytest.approx(17.0)


def test_unsorted_starts_are_sorted(clock):
    e = SyncEngine(clock=clock)
    e.set_lines([15.0, 5.0, 10.0])
    e.update(anchor(clock, 11.0))
    assert e.index == 1


def test_threshold_is_configurable(clock):
    e = SyncEngine(clock=clock, seek_threshold=0.5)
    e.set_lines(STARTS)
    e.update(anchor(clock, 6.0))
    assert e.update(anchor(clock, 7.0)).reason == "seek"
