"""A stand-in for the time module as one product module sees it.

A fixture that fakes sleeping and the monotonic clock must not fake them in
the time module itself: subprocess polls a child it waits for with
time.sleep (1 ms doubling to 50 ms per poll, looked up on the module at each
call), so a fake sleep there advanced the fake clock by whatever the poll
loop spun through until the child was reaped, a random amount that grows
with the host's load. In test_ios_virtual_iphone it moved the clock by 17 s
inside one call of the stand-in control plane, which shifted the start's
15 s poll cadence and made about one run in ten press the lock screen's side
button a cycle early. The product module's name `time` is bound to a
ProductClock instead; the time module keeps its own functions, and whatever
the clock does not fake (time.time) is the module's.
"""
import time


class ProductClock:
    def __init__(self, start=1000.0):
        self.now = start

    def sleep(self, seconds):
        self.now += max(0.0, seconds)

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)
