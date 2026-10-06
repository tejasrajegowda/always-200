"""The second CI fixture (ruling P11): a hand-built clean soak where the band must stay silent."""

from pathlib import Path

from always_200.bench.answer_key import read_key
from always_200.bench.score import score_session
from always_200.config import REPO_ROOT, graph_from, load_config
from always_200.core import Sample
from always_200.detect.band import BandConfig, BandDetector
from always_200.incidents import IncidentBuilder
from always_200.pipeline import run_detector
from always_200.sources.replay import ReplaySource
from tests.fixtures.soak_synthetic import make_soak

FOLDER = Path(__file__).parents[2] / "fixtures" / "soak_synthetic"
SOAK = FOLDER / "samples.jsonl"
KEY = read_key(FOLDER / "answer_key.jsonl")
SOAK_SCORED_START = KEY.scored_start


def _config() -> dict:
    """The default config now; the frozen one once step 18 has committed it."""
    return load_config(frozen=(REPO_ROOT / "config" / "frozen.yaml").exists())


def _band(cfg: dict) -> BandDetector:
    return BandDetector(BandConfig.from_config(cfg))


def test_band_raises_zero_false_alarms_on_the_synthetic_soak():
    cfg = _config()
    incidents = run_detector(
        ReplaySource(SOAK).samples(),
        _band(cfg),
        IncidentBuilder(graph_from(cfg)),
        scored_start=SOAK_SCORED_START,
    )
    assert [i for i in incidents if i.kind == "fault"] == []
    assert incidents == []  # no monitoring gap either: every step has its scrape
    score = score_session(incidents, KEY, graph_from(cfg))
    assert score.false_alarms == [] and score.clean_hours == 1.0


def test_the_fixture_is_exactly_what_the_generator_builds(tmp_path):
    make_soak.write(tmp_path)
    for name in ("samples.jsonl", "answer_key.jsonl"):
        assert (tmp_path / name).read_bytes() == (FOLDER / name).read_bytes(), name


def test_the_soak_really_follows_the_day_curve():
    """The zero above means something only if the soak is not flat: busier is slower."""
    rows = [s for s in ReplaySource(SOAK).samples() if s.service == "storefront"]
    rate = {s.ts: s.value for s in rows if s.signal == "request_rate"}
    p95 = {s.ts: s.value for s in rows if s.signal == "latency_p95"}
    assert max(rate.values()) > 2.5 * min(rate.values())
    busy = sorted(rate, key=rate.get)
    quiet_half, busy_half = busy[: len(busy) // 2], busy[len(busy) // 2 :]
    mean = lambda ts: sum(p95[t] for t in ts) / len(ts)  # noqa: E731
    assert mean(busy_half) > 1.15 * mean(quiet_half)


def test_the_same_soak_with_one_moderate_fault_is_caught():
    """The silence is not a dead detector: +100% latency on orders for 3 minutes fires."""
    cfg = load_config()
    start = KEY.scored_start + 1800  # midday, the busiest point of the soak
    end = start + 180

    def sick(s: Sample) -> Sample:
        if s.service == "orders" and s.signal == "latency_p95" and start <= s.ts < end:
            return Sample(s.ts, s.service, s.signal, s.value * 2.0)
        return s

    incidents = run_detector(
        (sick(s) for s in ReplaySource(SOAK).samples()),
        _band(cfg),
        IncidentBuilder(graph_from(cfg)),
        scored_start=SOAK_SCORED_START,
    )
    (inc,) = incidents
    assert inc.kind == "fault" and start <= inc.opened_at <= start + 3 * 15
    assert inc.culprit == "orders"
