(function () {
  "use strict";

  // Six steps every shop goes through (same order as #journey).
  var STEPS = ["등록", "동의", "인터뷰", "검수", "사장님 확인", "공개"];

  var form = document.getElementById("shop-form");
  var startBtn = document.getElementById("start-btn");
  var errorBox = document.getElementById("form-error");
  var sheetShop = document.getElementById("sheet-shop");
  var newShopBtn = document.getElementById("new-shop-btn");

  function shell() {
    return window.slShell || null;
  }

  function go(href) {
    var s = shell();
    if (s && s.go) {
      s.go(href, 1);
    } else {
      window.location.href = href;
    }
  }

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.hidden = false;
  }

  function clearError() {
    errorBox.textContent = "";
    errorBox.hidden = true;
  }

  function value(id) {
    return document.getElementById(id).value.trim();
  }

  function postJson(url, body) {
    return slAuth.apiFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }

  function messageFor(status, step) {
    if (status === 400) {
      return "소속 기관이 아직 없어요. 기관을 먼저 만들어 주세요.";
    }
    if (status === 422) {
      return "입력 내용을 다시 확인해 주세요. 업종이나 숫자 칸이 올바르지 않아요.";
    }
    if (status === 403) {
      return "이 기관에서 " + step + "을 할 권한이 없어요. 기관을 다시 골라 주세요.";
    }
    if (status === 404) {
      return "가게 정보를 찾지 못했어요. 처음부터 다시 등록해 주세요.";
    }
    if (status === 409) {
      return "이 가게의 인터뷰는 더 진행할 수 없어요. 예정된 횟수를 모두 마쳤거나 질문이 남지 않았어요.";
    }
    return step + " 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요. (" + status + ")";
  }

  function connectMessage(err) {
    var msg = err && err.message ? err.message : "";
    if (!msg || msg.indexOf("Failed to fetch") !== -1) {
      msg = "서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.";
    }
    return msg;
  }

  // ---- new shop sheet ----
  newShopBtn.addEventListener("click", function () {
    var s = shell();
    if (s && s.openSheet) {
      s.openSheet(sheetShop);
    } else {
      sheetShop.hidden = false;
    }
  });

  var regionSido = document.getElementById("region-sido");
  var regionSigungu = document.getElementById("region-sigungu");
  var regionError = document.getElementById("region-error");

  function addOption(select, val, text) {
    var opt = document.createElement("option");
    opt.value = val;
    opt.textContent = text;
    select.appendChild(opt);
    return opt;
  }

  function resetSelect(select, placeholder) {
    while (select.firstChild) {
      select.removeChild(select.firstChild);
    }
    addOption(select, "", placeholder);
  }

  function showRegionNote(msg) {
    regionError.textContent = msg;
    regionError.hidden = false;
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

  function loadSido() {
    fetchRegions(null)
      .then(function (rows) {
        rows.forEach(function (r) {
          addOption(regionSido, r.code, r.sido).setAttribute("data-sido", r.sido);
        });
      })
      .catch(function () {
        showRegionNote("지역 목록을 불러오지 못했어요. 지역 없이도 등록할 수 있어요.");
      });
  }

  regionSido.addEventListener("change", function () {
    var opt = regionSido.options[regionSido.selectedIndex];
    var sido = opt ? opt.getAttribute("data-sido") : null;
    regionError.hidden = true;
    if (!sido) {
      resetSelect(regionSigungu, "시·도를 먼저 골라 주세요");
      regionSigungu.disabled = true;
      return;
    }
    resetSelect(regionSigungu, "불러오는 중…");
    regionSigungu.disabled = true;
    fetchRegions(sido)
      .then(function (rows) {
        if (regionSido.value !== opt.value) {
          return;
        }
        resetSelect(regionSigungu, "시·도 전체");
        rows.forEach(function (r) {
          addOption(regionSigungu, r.code, r.sigungu);
        });
        regionSigungu.disabled = false;
      })
      .catch(function () {
        resetSelect(regionSigungu, "시·도 전체");
        showRegionNote("시·군·구 목록을 불러오지 못했어요. 시·도만으로도 등록할 수 있어요.");
      });
  });

  function selectedRegionCode() {
    return regionSigungu.value || regionSido.value || null;
  }

  loadSido();

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    clearError();

    var name = value("shop-name");
    var owner = value("owner-name");
    var yearsRaw = value("years");
    var closedRaw = value("closed-year");
    if (!name || !owner) {
      showError("가게 이름과 사장님 성함을 입력해 주세요.");
      return;
    }
    if (yearsRaw === "" || closedRaw === "") {
      showError("운영 연수와 폐업 연도를 숫자로 입력해 주세요.");
      return;
    }
    var years = Number(yearsRaw);
    var closedYear = Number(closedRaw);
    if (!Number.isInteger(years) || !Number.isInteger(closedYear)) {
      showError("운영 연수와 폐업 연도는 정수로 입력해 주세요.");
      return;
    }
    var suppliers = value("supplier-names")
      .split(",")
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });

    var payload = {
      name: name,
      owner_name: owner,
      trade: value("trade"),
      area_type: value("area-type"),
      years: years,
      closed_year: closedYear,
      supplier_names: suppliers
    };
    var regionCode = selectedRegionCode();
    if (regionCode) {
      payload.region_code = regionCode;
    }

    startBtn.disabled = true;
    postJson("/api/shops", payload)
      .then(function (res) {
        if (!res.ok) {
          throw new Error(messageFor(res.status, "가게 등록"));
        }
        return res.json();
      })
      .then(function (shop) {
        // The form held real names: clear it before leaving.
        form.reset();
        go("/shops/" + encodeURIComponent(shop.shop_id) + "/consent");
      })
      .catch(function (err) {
        showError(connectMessage(err));
        startBtn.disabled = false;
      });
  });

  // ---- shop list ----
  var AREA_LABELS = {
    residential: "주택가",
    station: "역세권",
    office: "오피스가",
    other: "기타"
  };
  var REASON_LABELS = {
    limit: "예정 회차 마침",
    exhausted: "질문 모두 마침"
  };

  var shopList = document.getElementById("shop-list");
  var shopListEmpty = document.getElementById("shop-list-empty");
  var listError = document.getElementById("list-error");

  function showListError(msg) {
    listError.textContent = msg;
    listError.hidden = false;
  }

  function clearListError() {
    listError.textContent = "";
    listError.hidden = true;
  }

  function shopCode(shop) {
    return String(shop.shop_id).slice(0, 6).toUpperCase();
  }

  function interviewHref(sessionId) {
    return "/interview/" + encodeURIComponent(sessionId);
  }

  function consentHref(shop) {
    return "/shops/" + encodeURIComponent(shop.shop_id) + "/consent";
  }

  function reviewHref(shop) {
    return "/review/" + encodeURIComponent(shop.card_id);
  }

  function shopTags(shop) {
    return [
      slLabels.tradeLabel(shop.trade),
      AREA_LABELS[shop.area_type] || shop.area_type,
      shop.years + "년 운영",
      shop.closed_year + "년 폐업"
    ].join(" · ");
  }

  // Where the shop is on the six-step journey: {done, cur, label}.
  // A shop that has any interview round has passed consent (the flow requires it).
  function stageOf(shop) {
    if (shop.card_status === "published") {
      return { done: 6, cur: 6, label: "공개됨" };
    }
    if (shop.card_id) {
      return { done: 3, cur: 3, label: "카드 초안" };
    }
    if (shop.open_session_id) {
      return { done: 2, cur: 2, label: "인터뷰 " + shop.next_number + "회차 진행 중" };
    }
    if (shop.sessions_done > 0) {
      return { done: 2, cur: 2, label: "인터뷰 " + shop.sessions_done + "회 마침" };
    }
    return { done: 1, cur: 1, label: "동의 받기 전" };
  }

  // The single most useful action for a shop: {text, href | start}.
  function actionOf(shop) {
    if (shop.open_session_id) {
      return { text: "진행 중 회차 이어가기", href: interviewHref(shop.open_session_id) };
    }
    if (shop.sessions_done === 0 && shop.can_continue) {
      return { text: "동의 받고 1회차 시작", href: consentHref(shop) };
    }
    if (shop.can_continue) {
      return { text: shop.next_number + "회차 시작", start: true };
    }
    return null;
  }

  function startNextRound(shop, btn) {
    clearListError();
    btn.disabled = true;
    postJson("/api/sessions", { shop_id: shop.shop_id })
      .then(function (res) {
        if (!res.ok) {
          throw new Error(messageFor(res.status, "인터뷰 시작"));
        }
        return res.json();
      })
      .then(function (session) {
        go(interviewHref(session.session_id));
      })
      .catch(function (err) {
        showListError(connectMessage(err));
        btn.disabled = false;
      });
  }

  function runAction(shop, act, btn) {
    if (act.href) {
      go(act.href);
    } else if (act.start) {
      startNextRound(shop, btn);
    }
  }

  function renderRail(stage) {
    var wrap = document.createElement("div");
    wrap.className = "rail-wrap";
    var rail = document.createElement("div");
    rail.className = "rail";
    rail.setAttribute("role", "img");
    rail.setAttribute("aria-label", "여섯 단계 중 " + stage.done + "단계 마침");
    STEPS.forEach(function (name, i) {
      var seg = document.createElement("i");
      if (i < stage.done) {
        seg.className = "d";
      } else if (i === stage.cur) {
        seg.className = "c";
      }
      seg.title = name;
      rail.appendChild(seg);
    });
    wrap.appendChild(rail);
    var lb = document.createElement("div");
    lb.className = "rail-lb";
    lb.setAttribute("aria-hidden", "true");
    var first = document.createElement("span");
    first.textContent = STEPS[0];
    var last = document.createElement("span");
    last.textContent = STEPS[STEPS.length - 1];
    lb.appendChild(first);
    lb.appendChild(last);
    wrap.appendChild(lb);
    return wrap;
  }

  function renderShop(shop) {
    var stage = stageOf(shop);
    var li = document.createElement("li");
    li.className = "shop";
    li.setAttribute("data-shop-id", shop.shop_id);
    li.setAttribute("data-stagger", "");

    var top = document.createElement("div");
    top.className = "shop-top";
    var code = document.createElement("strong");
    code.className = "shop-code";
    code.textContent = "가게 코드 ";
    var codeVal = document.createElement("span");
    codeVal.className = "code";
    codeVal.textContent = shopCode(shop);
    code.appendChild(codeVal);
    top.appendChild(code);
    var stageLabel = document.createElement("span");
    stageLabel.className = "shop-stage hint";
    stageLabel.textContent = stage.label;
    top.appendChild(stageLabel);
    li.appendChild(top);

    var tags = document.createElement("p");
    tags.className = "shop-tags tags";
    tags.textContent = shopTags(shop);
    li.appendChild(tags);

    li.appendChild(renderRail(stage));

    var progress = document.createElement("p");
    progress.className = "shop-progress hint";
    progress.textContent = "인터뷰 " + shop.sessions_done + "/" + shop.planned + "회";
    li.appendChild(progress);

    var actions = document.createElement("div");
    actions.className = "shop-act";

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn continue-btn";
    var act = actionOf(shop);
    if (act) {
      btn.textContent = act.text;
      btn.addEventListener("click", function () {
        runAction(shop, act, btn);
      });
    } else {
      btn.disabled = true;
      btn.textContent = REASON_LABELS[shop.reason] || "더 진행할 수 없어요";
    }
    actions.appendChild(btn);

    if (shop.card_id) {
      var link = document.createElement("a");
      link.className = "btn sec review-link";
      link.href = reviewHref(shop);
      link.textContent = "검수 화면";
      actions.appendChild(link);
    }

    li.appendChild(actions);
    return li;
  }

  // ---- next-up card: one thing to do first ----
  var nextUp = document.getElementById("next-up");
  var nextUpText = document.getElementById("next-up-text");
  var nextUpSub = document.getElementById("next-up-sub");
  var nextUpBtn = document.getElementById("next-up-btn");
  var nextUpAction = null;

  nextUpBtn.addEventListener("click", function () {
    if (nextUpAction) {
      nextUpAction();
    }
  });

  function setNextUp(title, code, sub, btnText, action) {
    while (nextUpText.firstChild) {
      nextUpText.removeChild(nextUpText.firstChild);
    }
    if (code) {
      nextUpText.appendChild(document.createTextNode("가게 "));
      var c = document.createElement("span");
      c.className = "code";
      c.textContent = code;
      nextUpText.appendChild(c);
      nextUpText.appendChild(document.createTextNode(" · "));
    }
    nextUpText.appendChild(document.createTextNode(title));
    nextUpSub.textContent = sub;
    nextUpBtn.textContent = btnText;
    nextUpBtn.disabled = false;
    nextUpAction = action;
    nextUp.hidden = false;
  }

  // Priority: open round > draft card to review > next round > consent > register.
  function pickNextUp(shops) {
    var i;
    for (i = 0; i < shops.length; i++) {
      if (shops[i].open_session_id) {
        return { shop: shops[i], kind: "open" };
      }
    }
    for (i = 0; i < shops.length; i++) {
      if (shops[i].card_id && shops[i].card_status !== "published") {
        return { shop: shops[i], kind: "review" };
      }
    }
    for (i = 0; i < shops.length; i++) {
      if (shops[i].sessions_done === 0 && shops[i].can_continue) {
        return { shop: shops[i], kind: "consent" };
      }
    }
    for (i = 0; i < shops.length; i++) {
      if (shops[i].can_continue) {
        return { shop: shops[i], kind: "next" };
      }
    }
    return null;
  }

  function openShopSheet() {
    newShopBtn.click();
  }

  function renderNextUp(shops) {
    var pick = pickNextUp(shops);
    if (!pick) {
      setNextUp(
        shops.length ? "새 가게 등록" : "첫 가게 등록",
        null,
        shops.length ? "지금 이어갈 가게가 없어요. 새 가게를 등록해 보세요." : "가게를 등록하고 사장님께 동의를 받으면 인터뷰를 시작할 수 있어요.",
        "새 가게 등록",
        openShopSheet
      );
      return newShopBtn;
    }
    var shop = pick.shop;
    var code = shopCode(shop);
    var meta = slLabels.tradeLabel(shop.trade) + " · " + (AREA_LABELS[shop.area_type] || shop.area_type);
    if (pick.kind === "open") {
      setNextUp(shop.next_number + "회차 인터뷰", code, meta + " · 진행 중인 회차가 있어요",
        "진행 중 회차 이어가기", function () { go(interviewHref(shop.open_session_id)); });
    } else if (pick.kind === "review") {
      setNextUp("카드 검수", code, meta + " · 공개까지 남은 일이 있어요",
        "검수 화면으로", function () { go(reviewHref(shop)); });
    } else if (pick.kind === "consent") {
      setNextUp("동의 받기", code, meta + " · 동의를 받으면 1회차를 시작해요",
        "동의 받기", function () { go(consentHref(shop)); });
    } else {
      setNextUp(shop.next_number + "회차 인터뷰", code, meta + " · 이어서 더 들어 볼 수 있어요",
        shop.next_number + "회차 시작", function () { startNextRound(shop, nextUpBtn); });
    }
    return nextUpBtn;
  }

  function loadShops() {
    clearListError();
    slAuth.apiFetch("/api/shops")
      .then(function (res) {
        if (!res.ok) {
          throw new Error("등록한 가게 목록을 불러오지 못했어요. (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (shops) {
        while (shopList.firstChild) {
          shopList.removeChild(shopList.firstChild);
        }
        shops.forEach(function (shop) {
          shopList.appendChild(renderShop(shop));
        });
        shopListEmpty.hidden = shops.length !== 0;
        var target = renderNextUp(shops);
        var s = shell();
        if (s && s.coach) {
          // Wait for the page enter slide so the ring is measured in place.
          window.setTimeout(function () {
            s.coach(target);
          }, 500);
        }
      })
      .catch(function (err) {
        showListError(connectMessage(err));
      });
  }

  loadShops();

  document.addEventListener("DOMContentLoaded", function () {
    if (window.GYMotion) {
      window.GYMotion.enterPage();
    }
  });
})();
