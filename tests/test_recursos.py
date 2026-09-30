from __future__ import annotations

from pathlib import Path

import pytest

from app.alerter import Alerter
from app.config import Settings, load_settings
from app.heads.recursos import DiskSample, RecursosHead, Sample, Sampler
from app.store import AlertStore
from tests.conftest import FakeClock, FakeNotifier

pytestmark = pytest.mark.anyio


class FakeSampler:
    def __init__(self) -> None:
        self.next: Sample = sample()

    def sample(self) -> Sample:
        return self.next


def sample(cpu: float = 10, ram: float = 4.0, disks: list[DiskSample] | None = None) -> Sample:
    return Sample(cpu_pct=cpu, ram_free_gb=ram, ram_total_gb=11.6, disks=disks or [])


@pytest.fixture
def setup(settings: Settings, notifier: FakeNotifier, clock: FakeClock):
    # Sin cooldown: cada disparo se envía y se ve en ``notifier.sent``.
    settings = settings.model_copy(update={"alert_cooldown_min": 0})
    alerter = Alerter(settings, AlertStore(), notifier, clock=clock)
    sampler = FakeSampler()
    head = RecursosHead(settings, alerter, sampler=sampler)  # type: ignore[arg-type]
    return head, sampler, alerter, notifier


async def test_cpu_needs_sustained_readings(setup) -> None:
    head, sampler, alerter, notifier = setup
    sampler.next = sample(cpu=95)
    await head.check()
    await head.check()
    assert not alerter.is_active("recursos:cpu")
    await head.check()
    assert alerter.is_active("recursos:cpu")
    assert "CPU al 95 % sostenida 1.5 min (umbral 85 %)" in notifier.sent[0]


async def test_cpu_spike_resets_counter(setup) -> None:
    head, sampler, alerter, _ = setup
    for cpu in (95, 95, 50, 95, 95):
        sampler.next = sample(cpu=cpu)
        await head.check()
    assert not alerter.is_active("recursos:cpu")


async def test_cpu_resolves_with_margin(setup) -> None:
    head, sampler, alerter, notifier = setup
    sampler.next = sample(cpu=95)
    for _ in range(3):
        await head.check()
    sampler.next = sample(cpu=82)  # por debajo del umbral pero dentro del margen
    await head.check()
    assert alerter.is_active("recursos:cpu")
    sampler.next = sample(cpu=40)
    await head.check()
    assert not alerter.is_active("recursos:cpu")
    assert "🟢 [RESUELTO]" in notifier.sent[-1]
    assert "CPU normalizada (40 %)" in notifier.sent[-1]


async def test_ram_low_is_critical_and_resolves(setup) -> None:
    head, sampler, alerter, notifier = setup
    sampler.next = sample(ram=0.8)
    await head.check()
    assert alerter.store.get("recursos:ram").level == "critical"
    assert "RAM libre 0.8 GB (umbral 1.5 GB)" in notifier.sent[0]
    sampler.next = sample(ram=1.6)  # por encima del umbral, dentro del margen
    await head.check()
    assert alerter.is_active("recursos:ram")
    sampler.next = sample(ram=1.9)
    await head.check()
    assert not alerter.is_active("recursos:ram")
    assert "RAM libre recuperada (1.9 GB)" in notifier.sent[-1]


async def test_disk_levels(setup) -> None:
    head, sampler, alerter, notifier = setup
    sampler.next = sample(disks=[DiskSample("/srv/extra", 900, 800, 100, 89)])
    await head.check()
    alert = alerter.store.get("recursos:disco:/srv/extra")
    assert alert.level == "warning"
    assert "Disco /srv/extra al 89 % (100 GB libres, umbral 85 %)" in notifier.sent[0]
    sampler.next = sample(disks=[DiskSample("/srv/extra", 900, 860, 40, 96)])
    await head.check()
    assert alert.level == "critical"
    sampler.next = sample(disks=[DiskSample("/srv/extra", 900, 700, 200, 78)])
    await head.check()
    assert not alerter.is_active("recursos:disco:/srv/extra")


async def test_unmounted_disk(setup) -> None:
    head, sampler, alerter, notifier = setup
    sampler.next = sample(disks=[DiskSample("/srv/archivos", error="no está montado")])
    await head.check()
    assert alerter.is_active("recursos:disco_ausente:/srv/archivos")
    assert "Disco /srv/archivos: no está montado" in notifier.sent[0]
    sampler.next = sample(disks=[DiskSample("/srv/archivos", 100, 10, 90, 10)])
    await head.check()
    assert not alerter.is_active("recursos:disco_ausente:/srv/archivos")


async def test_snapshot_and_health(setup) -> None:
    head, sampler, _, _ = setup
    assert head.snapshot is None
    assert not head.healthy()
    await head.check()
    assert head.snapshot == sampler.next
    assert head.healthy()


# --- Sampler real ---------------------------------------------------------------


def test_sampler_reads_this_machine(tmp_path: Path) -> None:
    (tmp_path / "vacio").mkdir()
    settings = load_settings({"HOST_ROOT": str(tmp_path), "DISKS": "/,/vacio,/no-existe"})
    s = Sampler(settings).sample()
    assert 0 <= s.cpu_pct <= 100
    assert 0 < s.ram_free_gb <= s.ram_total_gb
    root, empty, missing = s.disks
    assert root.error is None and root.total_gb > 0 and 0 <= root.percent <= 100
    assert empty.error == "no está montado"
    assert missing.error == "no existe"
