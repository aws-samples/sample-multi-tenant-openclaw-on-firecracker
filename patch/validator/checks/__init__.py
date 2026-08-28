from collections import OrderedDict

from checks.blindspots import (
    check_e1, check_e2, check_e3, check_e4, check_e5,
    check_e6, check_e7, check_e8, check_e9, check_e10,
)
from checks.consistency import check_a1, check_a2, check_a3, check_a4
from checks.channels import (
    check_f1, check_f2, check_f3, check_f4, check_f5, check_f6,
    check_f7, check_f8, check_f9, check_f10, check_f11,
)
from checks.controlplane import check_g1, check_g2, check_g3, check_g4
from checks.kitproof import (
    check_d1, check_d2, check_d3, check_d4, check_d5, check_d6, check_d7,
    check_d8, check_d9,
)
from checks.permissions import check_c1, check_c2, check_c3, check_c4
from checks.scale import check_b1, check_b2, check_b3, check_b4


CHECKS = OrderedDict([
    ("A1", ("A", check_a1)), ("A2", ("A", check_a2)),
    ("A3", ("A", check_a3)), ("A4", ("A", check_a4)),
    ("B1", ("B", check_b1)), ("B2", ("B", check_b2)),
    ("B3", ("B", check_b3)), ("B4", ("B", check_b4)),
    ("C1", ("C", check_c1)), ("C2", ("C", check_c2)),
    ("C3", ("C", check_c3)), ("C4", ("C", check_c4)),
    ("D1", ("D", check_d1)), ("D2", ("D", check_d2)),
    ("D3", ("D", check_d3)), ("D4", ("D", check_d4)),
    ("D5", ("D", check_d5)), ("D6", ("D", check_d6)),
    ("D7", ("D", check_d7)), ("D8", ("D", check_d8)),
    ("D9", ("D", check_d9)),
    ("E1", ("E", check_e1)), ("E2", ("E", check_e2)),
    ("E3", ("E", check_e3)), ("E4", ("E", check_e4)),
    ("E5", ("E", check_e5)), ("E6", ("E", check_e6)),
    ("E7", ("E", check_e7)), ("E8", ("E", check_e8)),
    ("E9", ("E", check_e9)), ("E10", ("E", check_e10)),
    ("F1", ("F", check_f1)), ("F2", ("F", check_f2)),
    ("F3", ("F", check_f3)), ("F4", ("F", check_f4)),
    ("F5", ("F", check_f5)), ("F6", ("F", check_f6)),
    ("F7", ("F", check_f7)), ("F8", ("F", check_f8)),
    ("F9", ("F", check_f9)), ("F10", ("F", check_f10)),
    ("F11", ("F", check_f11)),
    ("G1", ("G", check_g1)), ("G2", ("G", check_g2)),
    ("G3", ("G", check_g3)), ("G4", ("G", check_g4)),
])

OFFLINE_CHECKS = {
    "A1", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9",
    "E2", "E7", "E8", "E10", "F7",
}
