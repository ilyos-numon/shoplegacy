// Review inbox: every card of the org grouped by status chips, with the
// supported-sentence ratio bar. Shows shop codes only (no names).
(function () {
  "use strict";

  var AREA_LABELS = (window.slLabels && window.slLabels.area) || {};
  var LOW_RATIO = 0.8;

  var filterBox = document.getElementById("inbox-filter");
  var list = document.getElementById("inbox-list");
  var empty = document.getElementById("inbox-empty");
  var loading = document.getElementById("inbox-loading");
  var errorBox = document.getElementById("inbox-error");

  var items = [];
  var current = "all";

  function shell() {
    return window.slShell || null;
  }

  function decided(it) {
    return it.owner_decision !== null && it.owner_decision !== undefined;
  }

  // Client filters (plan §4.2).
  var FILTERS = {
    all: function () {
      return true;
    },
    draft: function (it) {
      return it.status === "draft" && !decided(it);
    },
    wait: function (it) {
      return it.owner_link_active && !decided(it) && it.status !== "published";
    },
    hold: function (it) {
      return it.status === "on_hold" || it.status === "rejected";
    },
    pub: function (it) {
      return it.status === "published";
    }
  };

  function badgeCount(all) {
    var n = 0;
    all.forEach(function (it) {
      if (it.status === "draft" && !decided(it)) {
        n += 1;
      } else if (decided(it) && it.status !== "published") {
        n += 1;
      }
    });
    return n;
  }

  function statusOf(it) {
    if (it.status === "published") {
      return { cls: "pub", label: "공개" };
    }
    if (it.status === "unpublished") {
      return { cls: "hold", label: "공개 중단" };
    }
    if (it.status === "on_hold") {
      return { cls: "hold", label: "보류" };
    }
    if (it.status === "rejected") {
      return { cls: "rej", label: "반려" };
    }
    if (it.owner_decision === "approve") {
      return { cls: "wait", label: "사장님 승인 도착" };
    }
    if (it.owner_decision === "request_changes") {
      return { cls: "wait", label: "사장님 수정 요청" };
    }
    if (it.owner_link_active) {
      return { cls: "wait", label: "사장님 답 대기" };
    }
    return { cls: "draft", label: "초안" };
  }

  function shopCode(it) {
    return String(it.shop_id || "").slice(0, 6).toUpperCase();
  }

  function tagsText(t) {
    if (!t) {
      return "";
    }
    var trade = window.slLabels ? window.slLabels.tradeLabel(t.trade) : t.trade;
    return [
      trade,
      AREA_LABELS[t.area_type] || t.area_type,
      t.region,
      t.years ? t.years + "년 운영" : ""
    ].filter(function (s) { return s; }).join(" · ");
  }

  function whenText(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) {
      return "";
    }
    var mins = Math.floor((Date.now() - d.getTime()) / 60000);
    if (mins < 1) {
      return "방금";
    }
    if (mins < 60) {
      return mins + "분 전";
    }
    if (mins < 60 * 24) {
      return Math.floor(mins / 60) + "시간 전";
    }
    return (d.getMonth() + 1) + "월 " + d.getDate() + "일";
  }

  function span(cls, text) {
    var s = document.createElement("span");
    if (cls) {
      s.className = cls;
    }
    s.textContent = text;
    return s;
  }

  function open(cardId) {
    var href = "/review/" + encodeURIComponent(cardId);
    var s = shell();
    if (s && s.go) {
      s.go(href, 1);
    } else {
      window.location.href = href;
    }
  }

  function renderItem(it) {
    var li = document.createElement("li");
    li.setAttribute("data-card-id", it.card_id);
    li.setAttribute("data-status", it.status);

    var b = document.createElement("button");
    b.type = "button";
    b.className = "inbox-item";

    var st = statusOf(it);
    var head = span("head", "");
    head.appendChild(span("", "가게 "));
    head.appendChild(span("code", shopCode(it)));
    head.appendChild(span("pill " + st.cls, st.label));
    b.appendChild(head);
    b.appendChild(span("hint when", whenText(it.updated_at)));

    var tags = tagsText(it.tags);
    if (tags) {
      b.appendChild(span("hint tags", tags));
    }

    var ratio = Math.max(0, Math.min(1, Number(it.support_ratio) || 0));
    var pct = Math.round(ratio * 100);
    var meter = span("meter" + (ratio < LOW_RATIO ? " warn" : ""), "");
    var row = span("row", "");
    row.appendChild(span("", "근거 있는 문장"));
    row.appendChild(span("pct", pct + "%"));
    meter.appendChild(row);
    var track = document.createElement("i");
    track.setAttribute("aria-hidden", "true");
    var fill = document.createElement("b");
    track.appendChild(fill);
    meter.appendChild(track);
    b.appendChild(meter);

    b.setAttribute("aria-label", "가게 " + shopCode(it) + ", " + st.label + ", 근거 있는 문장 " + pct + "%");
    b.addEventListener("click", function () {
      open(it.card_id);
    });
    li.appendChild(b);
    return { li: li, fill: fill, ratio: ratio };
  }

  function fillBar(fill, ratio) {
    if (window.GYMotion && window.GYMotion.fillBar) {
      window.GYMotion.fillBar(fill, ratio);
    } else {
      fill.style.transform = "scaleX(" + ratio + ")";
    }
  }

  function renderCounts() {
    Object.keys(FILTERS).forEach(function (k) {
      var n = items.filter(FILTERS[k]).length;
      var el = filterBox.querySelector('[data-count="' + k + '"]');
      if (el) {
        el.textContent = String(n);
      }
    });
  }

  function render() {
    list.textContent = "";
    var shown = items.filter(FILTERS[current]);
    var bars = shown.map(function (it) {
      var r = renderItem(it);
      list.appendChild(r.li);
      return r;
    });
    bars.forEach(function (r) {
      fillBar(r.fill, r.ratio);
    });
    empty.hidden = shown.length > 0;
  }

  filterBox.addEventListener("click", function (ev) {
    var btn = ev.target instanceof Element ? ev.target.closest("button[data-f]") : null;
    if (!btn) {
      return;
    }
    current = btn.getAttribute("data-f");
    Array.prototype.forEach.call(filterBox.querySelectorAll("button[data-f]"), function (x) {
      x.setAttribute("aria-pressed", x === btn ? "true" : "false");
    });
    render();
  });

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.hidden = false;
  }

  function load() {
    return slAuth.apiFetch("/api/review/inbox").then(function (res) {
      if (res.status === 403) {
        throw new Error("검수함은 검토자 이상만 볼 수 있어요.");
      }
      if (!res.ok) {
        throw new Error("카드 목록을 불러오지 못했어요. 잠시 후 다시 시도해 주세요.");
      }
      return res.json();
    }).then(function (data) {
      items = data && Array.isArray(data.items) ? data.items : [];
      loading.hidden = true;
      renderCounts();
      render();
      var s = shell();
      if (s && s.setBadge) {
        s.setBadge(badgeCount(items));
      }
      var first = list.querySelector(".inbox-item");
      if (first && s && s.coach) {
        // Wait for the page enter slide so the ring is measured in place.
        window.setTimeout(function () {
          s.coach(first);
        }, 500);
      }
    });
  }

  load().catch(function (err) {
    loading.hidden = true;
    var msg = err && err.message ? err.message : "";
    if (!msg || msg.indexOf("Failed to fetch") !== -1) {
      msg = "서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.";
    }
    showError(msg);
  });

  document.addEventListener("DOMContentLoaded", function () {
    if (window.GYMotion) {
      window.GYMotion.enterPage();
    }
  });
})();
