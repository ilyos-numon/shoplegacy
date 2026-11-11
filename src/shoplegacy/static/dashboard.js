(function () {
  "use strict";

  var ORG_KEY = "sl_org_id";
  var CONSENT_KINDS = ["recording", "storage", "ai_processing", "publication"];
  var CONSENT_LABELS = {
    recording: "녹음",
    storage: "기록 보관",
    ai_processing: "AI 처리",
    publication: "카드 공개"
  };
  var STATUS_ORDER = ["draft", "on_hold", "rejected", "published", "unpublished"];
  var STATUS_LABELS = {
    draft: "초안", on_hold: "보류", rejected: "반려", published: "공개", unpublished: "공개 취소"
  };

  var el = {
    from: document.getElementById("dash-from"),
    to: document.getElementById("dash-to"),
    apply: document.getElementById("dash-apply"),
    period: document.getElementById("dash-period"),
    error: document.getElementById("dash-error"),
    shops: document.getElementById("stat-shops"),
    interviews: document.getElementById("stat-interviews"),
    published: document.getElementById("stat-published"),
    consentBody: document.querySelector("#consent-table tbody"),
    statusBody: document.querySelector("#status-table tbody"),
    csv: document.getElementById("export-csv"),
    xlsx: document.getElementById("export-xlsx"),
    range: document.getElementById("dash-range"),
    custom: document.getElementById("dash-custom")
  };

  function motion() {
    return window.GYMotion || null;
  }

  function shell() {
    return window.slShell || null;
  }

  function orgId() {
    try {
      return window.localStorage.getItem(ORG_KEY);
    } catch (e) {
      return null;
    }
  }

  function showError(msg) {
    el.error.textContent = msg;
    el.error.hidden = false;
  }

  function clearError() {
    el.error.textContent = "";
    el.error.hidden = true;
  }

  function messageFor(status, detail) {
    if (status === 422 && detail === "from_after_to") {
      return "시작일이 종료일보다 늦어요. 기간을 다시 골라 주세요.";
    }
    if (status === 422) {
      return "날짜 형식을 다시 확인해 주세요.";
    }
    if (status === 403) {
      return "대시보드는 기관 관리자만 볼 수 있어요.";
    }
    return "대시보드를 불러오지 못했어요. 잠시 후 다시 시도해 주세요. (" + status + ")";
  }

  function query(fmt) {
    var params = new URLSearchParams();
    if (el.from.value) {
      params.set("from", el.from.value);
    }
    if (el.to.value) {
      params.set("to", el.to.value);
    }
    if (fmt) {
      params.set("format", fmt);
    }
    var qs = params.toString();
    return qs ? "?" + qs : "";
  }

  function apiBase() {
    return "/api/orgs/" + encodeURIComponent(orgId() || "");
  }

  function updateExportLinks() {
    el.csv.href = apiBase() + "/report" + query("csv");
    el.xlsx.href = apiBase() + "/report" + query("xlsx");
  }

  function numCell(row, value, extra) {
    var td = document.createElement("td");
    td.className = extra ? "num " + extra : "num";
    td.textContent = String(value);
    row.appendChild(td);
  }

  // Row header holds the label and a decorative bar; the number stays in its own cell.
  function headCell(row, text) {
    var th = document.createElement("th");
    th.scope = "row";
    var wrap = document.createElement("span");
    wrap.className = "hbar-row";
    var label = document.createElement("span");
    label.textContent = text;
    var track = document.createElement("i");
    track.setAttribute("aria-hidden", "true");
    var fill = document.createElement("b");
    track.appendChild(fill);
    wrap.appendChild(label);
    wrap.appendChild(track);
    th.appendChild(wrap);
    row.appendChild(th);
    return fill;
  }

  function fillBars(bars) {
    var max = 0;
    bars.forEach(function (b) {
      max = Math.max(max, b.value);
    });
    var m = motion();
    bars.forEach(function (b) {
      var ratio = max > 0 ? b.value / max : 0;
      if (m) {
        m.fillBar(b.el, ratio);
      } else {
        b.el.style.transform = "scaleX(" + ratio + ")";
      }
    });
  }

  function renderConsents(consents) {
    el.consentBody.textContent = "";
    var bars = [];
    CONSENT_KINDS.forEach(function (kind) {
      var c = (consents && consents[kind]) || { granted: 0, revoked: 0 };
      var tr = document.createElement("tr");
      tr.setAttribute("data-kind", kind);
      bars.push({ el: headCell(tr, CONSENT_LABELS[kind]), value: c.granted || 0 });
      numCell(tr, c.granted || 0);
      numCell(tr, c.revoked || 0, "revoked");
      el.consentBody.appendChild(tr);
    });
    fillBars(bars);
  }

  function renderStatuses(byStatus) {
    el.statusBody.textContent = "";
    var keys = STATUS_ORDER.slice();
    Object.keys(byStatus || {}).forEach(function (k) {
      if (keys.indexOf(k) < 0) {
        keys.push(k);
      }
    });
    var bars = [];
    keys.forEach(function (status) {
      var tr = document.createElement("tr");
      tr.setAttribute("data-status", status);
      var n = (byStatus && byStatus[status]) || 0;
      bars.push({ el: headCell(tr, STATUS_LABELS[status] || status), value: n });
      numCell(tr, n);
      el.statusBody.appendChild(tr);
    });
    fillBars(bars);
  }

  function countUp(node, value) {
    var n = Number(value) || 0;
    var m = motion();
    if (m) {
      m.countUp(node, n);
    } else {
      node.textContent = n.toLocaleString("ko-KR");
    }
  }

  function render(stats) {
    el.from.value = stats.period_from;
    el.to.value = stats.period_to;
    el.period.textContent = stats.period_from + " ~ " + stats.period_to + " 기준";
    countUp(el.shops, stats.shops);
    countUp(el.interviews, stats.interviews);
    countUp(el.published, stats.published_cards);
    renderConsents(stats.consents);
    renderStatuses(stats.cards_by_status);
    updateExportLinks();
  }

  function load() {
    clearError();
    if (!orgId()) {
      showError("기관 정보를 찾지 못했어요. 다시 로그인해 주세요.");
      return Promise.resolve();
    }
    if (el.from.value && el.to.value && el.from.value > el.to.value) {
      showError(messageFor(422, "from_after_to"));
      return Promise.resolve();
    }
    el.apply.disabled = true;
    return window.slAuth.apiFetch(apiBase() + "/dashboard" + query(null))
      .then(function (res) {
        return res.json().catch(function () {
          return {};
        }).then(function (body) {
          if (!res.ok) {
            showError(messageFor(res.status, body && body.detail));
            return;
          }
          render(body);
        });
      })
      .catch(function () {
        showError("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.");
      })
      .then(function () {
        el.apply.disabled = false;
      });
  }

  function filenameFrom(res, fallback) {
    var cd = res.headers.get("Content-Disposition") || "";
    var m = /filename="?([^";]+)"?/.exec(cd);
    return m ? m[1] : fallback;
  }

  function download(link, fmt) {
    clearError();
    updateExportLinks();
    window.slAuth.apiFetch(link.href)
      .then(function (res) {
        if (!res.ok) {
          return res.json().catch(function () {
            return {};
          }).then(function (body) {
            showError(messageFor(res.status, body && body.detail));
          });
        }
        return res.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = url;
          a.download = filenameFrom(res, "report." + fmt);
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(function () {
            URL.revokeObjectURL(url);
          }, 1000);
        });
      })
      .catch(function () {
        showError("보고서를 내려받지 못했어요. 잠시 후 다시 시도해 주세요.");
      });
  }

  function isoDay(d) {
    var mm = String(d.getMonth() + 1);
    var dd = String(d.getDate());
    return d.getFullYear() + "-" + (mm.length < 2 ? "0" + mm : mm) + "-" + (dd.length < 2 ? "0" + dd : dd);
  }

  function pressChip(chip) {
    Array.prototype.forEach.call(el.range.querySelectorAll("button"), function (b) {
      b.setAttribute("aria-pressed", b === chip ? "true" : "false");
    });
  }

  function pickRange(chip) {
    var kind = chip.getAttribute("data-days");
    pressChip(chip);
    if (kind === "custom") {
      el.custom.hidden = false;
      chip.setAttribute("aria-expanded", "true");
      el.from.focus();
      return;
    }
    var custom = el.range.querySelector('[data-days="custom"]');
    custom.setAttribute("aria-expanded", "false");
    el.custom.hidden = true;
    var days = parseInt(kind, 10) || 30;
    var end = new Date();
    var start = new Date(end.getFullYear(), end.getMonth(), end.getDate() - (days - 1));
    el.from.value = isoDay(start);
    el.to.value = isoDay(end);
    load();
  }

  Array.prototype.forEach.call(el.range.querySelectorAll("button"), function (chip) {
    chip.addEventListener("click", function () {
      pickRange(chip);
    });
  });
  el.range.querySelector('[data-days="custom"]').setAttribute("aria-expanded", "false");

  el.apply.addEventListener("click", load);
  el.from.addEventListener("change", updateExportLinks);
  el.to.addEventListener("change", updateExportLinks);
  el.csv.addEventListener("click", function (ev) {
    ev.preventDefault();
    download(el.csv, "csv");
  });
  el.xlsx.addEventListener("click", function (ev) {
    ev.preventDefault();
    download(el.xlsx, "xlsx");
  });

  renderConsents(null);
  renderStatuses(null);
  window.slAuth.requireLogin().then(function () {
    // apiFetch fills sl_org_id from /api/me on a fresh login before the first call.
    return window.slAuth.getToken();
  }).then(function (token) {
    if (!token) {
      return null;
    }
    if (orgId()) {
      updateExportLinks();
      return load();
    }
    return window.slAuth.apiFetch("/api/me").then(function () {
      updateExportLinks();
      return load();
    });
  }).then(function () {
    var s = shell();
    if (!s || !s.coach || !el.error.hidden) {
      return;
    }
    // Wait for the page enter slide so the ring is measured in place.
    window.setTimeout(function () {
      s.coach(el.csv);
    }, 500);
  });

  document.addEventListener("DOMContentLoaded", function () {
    if (motion()) {
      motion().enterPage();
    }
  });
})();
