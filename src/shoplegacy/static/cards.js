(function () {
  "use strict";

  var AXES = ["location", "suppliers", "menu", "pricing", "customers", "failures"];
  var AREA_LABELS = { residential: "주택가", station: "역세권", office: "오피스가", other: "기타 지역" };
  var EXCERPT_LEN = 80;

  var el = {
    trade: document.getElementById("filter-trade"),
    area: document.getElementById("filter-area"),
    sido: document.getElementById("filter-sido"),
    sigungu: document.getElementById("filter-sigungu"),
    q: document.getElementById("filter-q"),
    btn: document.getElementById("search-btn"),
    regionBtn: document.getElementById("region-btn"),
    sheet: document.getElementById("sheet-region"),
    count: document.getElementById("result-count"),
    list: document.getElementById("card-list"),
    empty: document.getElementById("empty"),
    error: document.getElementById("list-error")
  };

  function showError(msg) {
    el.error.textContent = msg;
    el.error.hidden = false;
  }

  function clearError() {
    el.error.textContent = "";
    el.error.hidden = true;
  }

  function messageFor(status) {
    if (status === 422) {
      return "검색 조건을 다시 확인해 주세요. 고를 수 없는 값이 들어 있어요.";
    }
    return "카드 목록을 불러오지 못했어요. 잠시 뒤 다시 시도해 주세요. (" + status + ")";
  }

  function hasOption(select, value) {
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === value) {
        return true;
      }
    }
    return false;
  }

  function tagLine(t) {
    return [
      slLabels.tradeLabel(t.trade),
      AREA_LABELS[t.area_type] || t.area_type,
      t.years + "년 운영",
      t.closed_year + "년 폐업"
    ].join(" · ");
  }

  function excerpt(sections) {
    for (var i = 0; i < AXES.length; i++) {
      var text = ((sections && sections[AXES[i]]) || "").trim();
      if (text) {
        var para = text.split(/\n\s*\n/)[0].trim();
        return para.length > EXCERPT_LEN ? para.slice(0, EXCERPT_LEN) + "…" : para;
      }
    }
    return "";
  }

  function renderItem(card) {
    var item = document.createElement("article");
    item.className = "card-item";
    item.setAttribute("data-card-id", card.card_id);

    var link = document.createElement("a");
    link.className = "card-link";
    link.href = "/cards/" + encodeURIComponent(card.card_id);
    link.textContent = "이야기 읽기";

    var tags = document.createElement("p");
    tags.className = "card-tags tags";
    tags.textContent = tagLine(card.tags);
    if (card.tags && card.tags.region) {
      var region = document.createElement("span");
      region.className = "tag-region";
      region.textContent = card.tags.region;
      tags.appendChild(document.createTextNode(" · "));
      tags.appendChild(region);
    }

    var ex = document.createElement("blockquote");
    ex.className = "card-excerpt";
    ex.textContent = excerpt(card.sections);

    item.appendChild(tags);
    if (ex.textContent) {
      item.appendChild(ex);
    }
    item.appendChild(link);
    return item;
  }

  function render(cards) {
    while (el.list.firstChild) {
      el.list.removeChild(el.list.firstChild);
    }
    cards.forEach(function (card) {
      el.list.appendChild(renderItem(card));
    });
    el.count.textContent = "카드 " + cards.length + "장";
    el.empty.hidden = cards.length !== 0;
    if (window.gsap && cards.length) {
      window.gsap.from(el.list.children, { y: 10, opacity: 0, stagger: 0.05, duration: 0.35, clearProps: "all" });
    }
  }

  function addOption(select, value, text) {
    var opt = document.createElement("option");
    opt.value = value;
    opt.textContent = text;
    select.appendChild(opt);
    return opt;
  }

  function resetSigungu() {
    while (el.sigungu.firstChild) {
      el.sigungu.removeChild(el.sigungu.firstChild);
    }
    addOption(el.sigungu, "", "전체");
    el.sigungu.disabled = true;
  }

  function fetchRegions(sido) {
    var url = "/api/regions" + (sido ? "?sido=" + encodeURIComponent(sido) : "");
    return fetch(url).then(function (res) {
      if (!res.ok) {
        throw new Error("regions " + res.status);
      }
      return res.json();
    });
  }

  function selectedSidoName() {
    var opt = el.sido.options[el.sido.selectedIndex];
    return opt ? opt.getAttribute("data-sido") : null;
  }

  // Fills the si/gun/gu list for the chosen si/do; resolves once the options are in place.
  function loadSigungu() {
    var sido = selectedSidoName();
    resetSigungu();
    if (!sido) {
      return Promise.resolve();
    }
    var wanted = el.sido.value;
    return fetchRegions(sido)
      .then(function (rows) {
        if (el.sido.value !== wanted) {
          return;
        }
        rows.forEach(function (r) {
          addOption(el.sigungu, r.code, r.sigungu);
        });
        el.sigungu.disabled = false;
      })
      .catch(function () {
        showError("시·군·구 목록을 불러오지 못했어요. 시·도로만 찾을 수 있어요.");
      });
  }

  function loadSido() {
    return fetchRegions(null)
      .then(function (rows) {
        rows.forEach(function (r) {
          addOption(el.sido, r.code, r.sido).setAttribute("data-sido", r.sido);
        });
      })
      .catch(function () {
        showError("지역 목록을 불러오지 못했어요. 업종과 동네 유형으로는 찾을 수 있어요.");
      });
  }

  function filterParams() {
    var params = new URLSearchParams();
    if (el.trade.value) {
      params.set("trade", el.trade.value);
    }
    if (el.area.value) {
      params.set("area_type", el.area.value);
    }
    var region = el.sigungu.value || el.sido.value;
    if (region) {
      params.set("region", region);
    }
    var q = el.q.value.trim();
    if (q) {
      params.set("q", q.slice(0, 50));
    }
    return params;
  }

  function syncAddress(params) {
    var qs = params.toString();
    window.history.replaceState(null, "", window.location.pathname + (qs ? "?" + qs : ""));
  }

  // Marks the region chip when a si/gun/gu or a place name narrows the list.
  function markRegionChip() {
    var on = !!(el.sigungu.value || el.q.value.trim());
    el.regionBtn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  var searchSeq = 0;

  function search() {
    clearError();
    markRegionChip();
    var params = filterParams();
    syncAddress(params);
    var seq = ++searchSeq;
    var apiParams = new URLSearchParams(params);
    apiParams.set("status", "published");
    el.btn.disabled = true;
    fetch("/api/cards?" + apiParams.toString())
      .then(function (res) {
        if (!res.ok) {
          throw new Error(messageFor(res.status));
        }
        return res.json();
      })
      .then(function (cards) {
        if (seq === searchSeq) {
          render(cards);
        }
      })
      .catch(function (err) {
        var msg = err && err.message ? err.message : "";
        if (!msg || msg.indexOf("Failed to fetch") !== -1) {
          msg = "서버에 연결하지 못했어요. 잠시 뒤 다시 시도해 주세요.";
        }
        showError(msg);
      })
      .then(function () {
        el.btn.disabled = false;
      });
  }

  function applyAddress() {
    var params = new URLSearchParams(window.location.search);
    var trade = params.get("trade");
    var area = params.get("area_type");
    if (trade !== null && hasOption(el.trade, trade)) {
      el.trade.value = trade;
    }
    if (area !== null && hasOption(el.area, area)) {
      el.area.value = area;
    }
    var q = params.get("q");
    if (q !== null) {
      el.q.value = q;
    }
    var region = params.get("region");
    if (region && /^[0-9]{2,10}$/.test(region)) {
      var sidoCode = null;
      for (var i = 0; i < el.sido.options.length; i++) {
        var v = el.sido.options[i].value;
        if (v && v.slice(0, 2) === region.slice(0, 2)) {
          sidoCode = v;
        }
      }
      if (sidoCode) {
        el.sido.value = sidoCode;
        return loadSigungu().then(function () {
          if (region !== sidoCode && hasOption(el.sigungu, region)) {
            el.sigungu.value = region;
          }
        });
      }
    }
    return Promise.resolve();
  }

  function closeRegionSheet() {
    if (window.slShell) {
      window.slShell.closeSheet(el.sheet);
    } else {
      el.sheet.hidden = true;
    }
  }

  el.regionBtn.addEventListener("click", function () {
    if (window.slShell) {
      window.slShell.openSheet(el.sheet);
    } else {
      el.sheet.hidden = false;
    }
  });
  el.btn.addEventListener("click", function () {
    closeRegionSheet();
    search();
  });
  el.trade.addEventListener("change", search);
  el.area.addEventListener("change", search);
  el.sido.addEventListener("change", function () {
    loadSigungu();
    search();
  });
  el.q.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") {
      ev.preventDefault();
      closeRegionSheet();
      search();
    }
  });

  if (window.GYMotion) {
    window.GYMotion.enterPage();
  }

  loadSido()
    .then(applyAddress)
    .then(search);
})();
