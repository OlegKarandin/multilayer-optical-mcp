from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, ROADM, Lightpath, Router, IPLink, Service,
    SRLG, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.views import (
    topology_dict, lightpaths_dict, services_dict,
    traffic_matrix_dict, srlgs_dict, risk_groups_dict,
    validation_report_dict,
)
from multilayer_optical_mcp.model.validate import (
    Violation, ValidationReport, ViolationType,
)
from multilayer_optical_mcp.model.violations import (
    ModeInfeasibleViolation, SpectrumClashViolation, IpLinkOverloadViolation,
    DroppedTrafficViolation, DisjointnessCollapseViolation,
    ProtectionNotViableViolation, ProtectionOversubscribedViolation,
    InvalidPlanViolation,
)


def _seed():
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    for node in ("A", "B"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                  elements=("roadm_A", "amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp1"))
    n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                          demand_gbps=10.0, working_path=("ip1",)))
    n.add_service(Service(id="svc2", src_router="R1", dst_router="R2",
                          demand_gbps=15.0, working_path=("ip1",)))
    n.add_srlg(SRLG(id="srlg-pole-A", asset_ids=("f1", "amp1")))
    n.define_risk_group(rg_id="rg-storm", asset_ids=("f1",),
                        metadata={"source": "operator"})
    return n


def test_topology_optical_only():
    n = _seed()
    t = topology_dict(n, layer="optical")
    assert "fibers" in t and "amplifiers" in t and "oms" in t
    assert "ip_links" not in t and "routers" not in t
    assert any(f["id"] == "f1" for f in t["fibers"])


def test_topology_ip_only():
    n = _seed()
    t = topology_dict(n, layer="ip")
    assert "ip_links" in t and "routers" in t
    assert "fibers" not in t


def test_topology_both_layers():
    n = _seed()
    t = topology_dict(n, layer="both")
    assert "fibers" in t and "ip_links" in t


def test_lightpaths_dict_carries_mode_and_path_and_qot_when_available():
    n = _seed()
    n.set_qot_state("lp1", QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=8.0,
                                    limiting_element_id="amp1"))
    lps = lightpaths_dict(n)
    assert len(lps) == 1
    entry = lps[0]
    assert entry["id"] == "lp1"
    assert entry["mode_id"] == "100G-QPSK"
    assert entry["oms_sequence"] == ["oms1"]
    assert entry["qot"]["margin_db"] == 8.0
    assert entry["qot"]["limiting_element_id"] == "amp1"


def test_lightpaths_dict_qot_is_none_when_not_observed():
    n = _seed()
    lps = lightpaths_dict(n)
    assert lps[0]["qot"] is None


def test_services_dict_carries_grooming_map():
    n = _seed()
    s = services_dict(n)
    assert "services" in s and "grooming_map" in s
    # Both services ride lp1 — grooming_map should group them.
    assert sorted(s["grooming_map"]["lp1"]) == ["svc1", "svc2"]


def test_traffic_matrix_aggregates_across_services():
    n = _seed()
    tm = traffic_matrix_dict(n)
    assert tm["R1"]["R2"] == 25.0  # 10 + 15


def test_srlgs_dict():
    n = _seed()
    s = srlgs_dict(n)
    assert s[0]["id"] == "srlg-pole-A"
    assert s[0]["asset_ids"] == ["f1", "amp1"]


def test_risk_groups_dict_carries_metadata():
    n = _seed()
    r = risk_groups_dict(n)
    assert r[0]["id"] == "rg-storm"
    assert r[0]["metadata"]["source"] == "operator"


# ---------------------------------------------------------------------------
# New tests for Task 7 serializers
# ---------------------------------------------------------------------------
from multilayer_optical_mcp.model.assets import Service
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model import views
from tests.model.test_ip_routing import _two_link_model


def _model_with_services():
    n = _two_link_model()
    n.add_service(Service(id="svc-AC", src_router="R-A", dst_router="R-C",
                          demand_gbps=120.0, working_path=("ipAB", "ipBC")))
    return n


def test_ip_topology_dict_annotates_capacity_and_load():
    n = _model_with_services()
    d = views.ip_topology_dict(n)
    links = {l["id"]: l for l in d["ip_links"]}
    assert links["ipAB"]["lightpath_id"] == "lpAB"
    assert links["ipAB"]["capacity_gbps"] == 200.0
    assert links["ipAB"]["load_gbps"] == 120.0
    assert {r["id"] for r in d["routers"]} == {"R-A", "R-B", "R-C"}


def test_ip_topology_dict_capacity_null_when_no_qot():
    n = _two_link_model()
    # Wipe one lightpath's QoT so capacity is unknown, not a crash.
    n._qot_state.pop("lpAB")
    d = views.ip_topology_dict(n)
    links = {l["id"]: l for l in d["ip_links"]}
    assert links["ipAB"]["capacity_gbps"] is None


def test_grooming_map_dict_both_directions():
    n = _model_with_services()
    d = views.grooming_map_dict(n)
    assert d["by_service"]["svc-AC"] == ["lpAB", "lpBC"]
    assert d["by_lightpath"]["lpAB"] == ["svc-AC"]


def test_ip_routing_result_dict_shape():
    n = _model_with_services()
    from multilayer_optical_mcp.model import ip_routing
    d = views.ip_routing_result_dict(ip_routing.simulate_ip_routing(n))
    assert set(d) == {"utilizations", "congestion", "restored", "dropped"}
    u = {x["ip_link_id"]: x for x in d["utilizations"]}
    assert u["ipAB"]["utilization"] == 0.6
    assert d["congestion"] == []
    assert d["dropped"]["services"] == []
    assert d["dropped"]["overflow_gbps"] == 0.0
    assert d["dropped"]["down_links"] == []


def test_affected_services_dict_shape():
    n = _model_with_services()
    d = views.affected_services_dict(n, "lpBC")
    assert d == {"asset_id": "lpBC", "services": ["svc-AC"]}


def test_validation_report_dict_flattens_detail_for_every_violation_type():
    """Regression guard: validate.py's finding-builder detail-dict keys and
    violations.py's Pydantic model fields must stay in lock-step. If someone
    renames a key in one place and not the other, this test catches it --
    model_validate raises on a missing/extra required field."""
    cases = [
        (Violation(ViolationType.MODE_INFEASIBLE, 0, "lpAB", False, {
            "margin_db": -1.0, "gsnr_db": 14.0, "required_gsnr_db": 15.0,
            "deficit_db": 1.0, "feasible_downshift_modes": ["50G-QPSK"],
        }), ModeInfeasibleViolation),
        (Violation(ViolationType.SPECTRUM_CLASH, 0, "omsAB", False, {
            "slot": 3, "lightpaths": ["lp1", "lp2"],
            "retune_candidates": {"lp1": [4], "lp2": []},
        }), SpectrumClashViolation),
        (Violation(ViolationType.IP_LINK_OVERLOAD, 0, "ipAB", False, {
            "utilization": 1.1, "offered_gbps": 110.0, "capacity_gbps": 100.0,
            "offload_gbps": 10.0,
        }), IpLinkOverloadViolation),
        (Violation(ViolationType.DROPPED_TRAFFIC, 0, "svc1", False, {
            "reason": "link_down", "on_link": "ipAB", "demand_gbps": 50.0,
        }), DroppedTrafficViolation),
        (Violation(ViolationType.DISJOINTNESS_COLLAPSE, 0, "svc1", False, {
            "basis": "risk_group", "level": "link",
            "shared_assets": ["fAB"], "shared_groups": ["rg1"],
        }), DisjointnessCollapseViolation),
        (Violation(ViolationType.PROTECTION_NOT_VIABLE, 0, "svc1", False, {
            "demand_gbps": 50.0, "protection_capacity_gbps": 0.0,
            "dead_links": ["ipCD"], "bottleneck_link": None,
        }), ProtectionNotViableViolation),
        (Violation(ViolationType.PROTECTION_OVERSUBSCRIBED, 0, "ipAB", False, {
            "working_gbps": 60.0, "reserved_gbps": 50.0, "capacity_gbps": 100.0,
            "overflow_gbps": 10.0, "reserving_services": ["svc1", "svc2"],
        }), ProtectionOversubscribedViolation),
        (Violation(ViolationType.INVALID_PLAN, 0, None, False, {
            "message": "unknown op 'frobnicate'", "op_index": 2,
        }), InvalidPlanViolation),
    ]
    for violation, model_cls in cases:
        report = ValidationReport(violations=(violation,), num_states=1)
        out = validation_report_dict(report)
        flat = out["violations"][0]
        assert "detail" not in flat, f"{model_cls.__name__}: detail must be flattened"
        # Must validate cleanly against the matching Pydantic model -- this is
        # the drift guard.
        instance = model_cls.model_validate(flat)
        assert instance.type == violation.type.value
        # model_validate alone only catches a missing/renamed key (raises
        # ValidationError); it silently accepts an *added* key that model_cls
        # doesn't declare (Pydantic drops unknown fields by default), so also
        # assert the key sets match exactly to catch that drift direction too.
        assert set(flat) == set(model_cls.model_fields), \
            f"{model_cls.__name__}: dict keys and model fields drifted"


def test_validation_report_dict_sanitizes_nonfinite_floats_when_flattened():
    violation = Violation(ViolationType.MODE_INFEASIBLE, 0, "lpAB", False, {
        "margin_db": float("-inf"), "gsnr_db": float("-inf"),
        "required_gsnr_db": 15.0, "deficit_db": float("inf"),
        "feasible_downshift_modes": [],
    })
    report = ValidationReport(violations=(violation,), num_states=1)
    out = validation_report_dict(report)
    flat = out["violations"][0]
    assert flat["margin_db"] == "-Infinity"
    assert flat["gsnr_db"] == "-Infinity"
    assert flat["deficit_db"] == "Infinity"
    assert flat["feasible_downshift_modes"] == []
