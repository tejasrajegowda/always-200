"""The fire/clear state machine every detector shares (r2 §8, ruling P1).

A detector decides, step by step, whether one series is "out" (out of its normal band, over its
threshold, ...). Deciding when that becomes an alert is the same for every detector, so it lives
here once and every detector is debounced identically:

- **fire** after `n_fire` consecutive out steps (N-in-a-row debouncing): one odd step is noise,
  three in a row is a problem;
- **clear** after `n_clear` consecutive in steps; any out step during that countdown restarts it;
- **emit** only on out steps while alerting. The in-band countdown steps emit nothing, but the
  series still counts as alerting until the clear, so a detector keeps its baseline frozen (P1).

It holds no clock: it counts the steps it is given, so it reads sample time only (D5).
"""


class FireClear:
    """Shared by every detector: fire after n_fire consecutive 'out' steps, clear after n_clear
    consecutive 'in' steps. Emits only on out-of-band steps while alerting; the in-band countdown
    steps emit nothing, though `alerting` stays True until the clear (P1)."""

    def __init__(self, n_fire: int = 3, n_clear: int = 4) -> None:
        if n_fire < 1 or n_clear < 1:
            raise ValueError("n_fire and n_clear must both be at least 1")
        self._n_fire = n_fire
        self._n_clear = n_clear
        self._alerting = False
        self._outs = 0  # consecutive out steps while not alerting
        self._ins = 0  # consecutive in steps while alerting (the clear countdown)

    @property
    def alerting(self) -> bool:
        """True from the firing step until the clear, countdown included."""
        return self._alerting

    def step(self, out: bool) -> tuple[bool, bool]:
        """One step; returns (emit_anomaly, first_step_of_alert).

        emit_anomaly is `alerting and out` after this step; first_step_of_alert is True only on
        the step that fires.
        """
        if not self._alerting:
            if not out:
                self._outs = 0
                return False, False
            self._outs += 1
            if self._outs < self._n_fire:
                return False, False
            self._alerting, self._outs, self._ins = True, 0, 0
            return True, True
        if out:
            self._ins = 0
            return True, False
        self._ins += 1
        if self._ins >= self._n_clear:
            self._alerting, self._ins = False, 0
        return False, False
