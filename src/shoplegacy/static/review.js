(function () {
  "use strict";

  var AXES = ["location", "suppliers", "menu", "pricing", "customers", "failures"];
  var AREA_LABELS = { residential: "주택가", station: "역세권", office: "오피스가", other: "기타 지역" };

  var parts = window.location.pathname.split("/").filter(function (s) { return s.length > 0; });
  var cardId = decodeURIComponent(parts[parts.length - 1] || "");
  var apiUrl = "/api/cards/" + encodeURIComponent(cardId);
  var publicUrl = "/cards/" + encodeURIComponent(cardId);

  var el = {
    meta: document.getElementById("card-meta"),
    warnings: document.getElementById("warnings"),
    save: document.getElementById("save-btn"),
    publish: document.getElementById("publish-btn"),
    message: document.getElementById("message"),
    error: document.getElementById("error"),
    link: document.getElementById("public-link"),
    linkA: document.getElementById("public-link-a"),
    hold: document.getElementById("hold-btn"),
    reject: document.getElementById("reject-btn"),
    regenerate: document.getElementById("regenerate-btn"),
    unpublish: document.getElementById("unpublish-btn"),
    status: document.getElementById("card-status"),
    support: document.getElementById("support-summary"),
    reasons: document.getElementById("quality-reasons"),
    note: document.getElementById("status-note"),
    currentNote: document.getElementById("current-note"),
    sources: document.getElementById("sources-list"),
    sourcesEmpty: document.getElementById("sources-empty"),
    llmBadge: document.getElementById("llm-badge"),
    llmWarning: document.getElementById("llm-warning"),
    ownerDecision: document.getElementById("owner-decision"),
    ownerDecisionMeta: document.getElementById("owner-decision-meta"),
    ownerPhone: document.getElementById("owner-phone"),
    ownerLinkBtn: document.getElementById("owner-link-btn"),
    ownerLinkError: document.getElementById("owner-link-error"),
    ownerLinkResult: document.getElementById("owner-link-result"),
    ownerLinkNote: document.getElementById("owner-link-note"),
    ownerLinkUrl: document.getElementById("owner-link-url"),
    ownerLinkCopy: document.getElementById("owner-link-copy"),
    ownerLinkExpires: document.getElementById("owner-link-expires"),
    ownerLinksList: document.querySelector("#owner-links ul"),
    ownerLinksEmpty: document.getElementById("owner-links-empty"),
    ownerLinksError: document.getElementById("owner-links-error"),
    fixlist: document.getElementById("fixlist"),
    steps: document.getElementById("rv-steps"),
    gateText: document.getElementById("gate-text"),
    gateGo: document.getElementById("gate-go"),
    more: document.getElementById("more-btn"),
    sheetMore: document.getElementById("sheet-more"),
    sheetSource: document.getElementById("sheet-source"),
    srcTitle: document.getElementById("src-title"),
    srcFocus: document.getElementById("src-focus"),
    warningsBox: document.getElementById("warnings-box"),
    stamp: document.getElementById("stamp")
  };
  var TABS = ["edit", "owner", "publish"];
  var STATUS_PILLS = {
    draft: "draft", on_hold: "hold", rejected: "rej", published: "pub", unpublished: "hold"
  };
  var currentReasons = [];
  var currentTab = "edit";
  var pendingAnswerId = null;
  var sourcesLoaded = false;
  var wide = window.matchMedia ? window.matchMedia("(min-width: 900px)") : null;

  function motion() {
    return window.GYMotion || null;
  }

  function openSheet(sheet) {
    if (window.slShell && slShell.openSheet) {
      slShell.openSheet(sheet);
    } else {
      sheet.hidden = false;
    }
  }

  function closeSheet(sheet) {
    if (sheet.hidden) {
      return;
    }
    if (window.slShell && slShell.closeSheet) {
      slShell.closeSheet(sheet);
    } else {
      sheet.hidden = true;
    }
  }

  var AXIS_LABELS = {
    location: "입지", suppliers: "거래처", menu: "메뉴",
    pricing: "가격", customers: "손님", failures: "실패"
  };
  var STATUS_LABELS = {
    draft: "초안", on_hold: "보류", rejected: "반려", published: "공개", unpublished: "공개 취소"
  };
  // Allowed status transitions (mirrors the server); buttons follow it so a click never asks for a 409.
  var TRANSITIONS = {
    draft: ["on_hold", "rejected", "published"],
    on_hold: ["draft", "rejected", "published"],
    rejected: ["draft"],
    published: ["unpublished"],
    unpublished: ["draft", "published"]
  };
  var DONE_MESSAGES = {
    on_hold: "보류했어요.",
    rejected: "반려했어요.",
    unpublished: "공개를 취소했어요. 고친 뒤 다시 공개할 수 있어요."
  };
  var currentCard = null;

  var REASON_NOTES = {
    limit: "예정 회차를 모두 마쳤습니다.",
    exhausted: "질문을 모두 마쳤습니다."
  };

  var next = {
    box: document.getElementById("next-actions"),
    btn: document.getElementById("next-round-btn"),
    note: document.getElementById("next-round-note")
  };
  var nextShopId = null;

  function nextRoundMessage(status) {
    if (status === 409) {
      return "이 가게의 인터뷰는 더 진행할 수 없습니다. 예정된 횟수를 모두 마쳤거나 질문이 남지 않았습니다.";
    }
    if (status === 404) {
      return "가게 정보를 찾지 못했습니다.";
    }
    return "다음 회차를 시작하지 못했습니다. 잠시 후 다시 시도해 주세요. (" + status + ")";
  }

  function loadProgress(shopId) {
    slAuth.apiFetch("/api/shops/" + encodeURIComponent(shopId) + "/progress")
      .then(function (res) {
        if (!res.ok) {
          return null;
        }
        return res.json();
      })
      .then(function (summary) {
        if (!summary) {
          return;
        }
        if (summary.can_continue) {
          next.btn.disabled = false;
          next.note.textContent = "";
        } else {
          next.btn.disabled = true;
          next.note.textContent = REASON_NOTES[summary.reason] || "";
        }
      })
      .catch(function () {
        next.btn.disabled = false;
      });
  }

  function setupNextActions(card) {
    nextShopId = card.shop_id;
    next.box.hidden = false;
    loadProgress(card.shop_id);
  }

  next.btn.addEventListener("click", function () {
    if (!nextShopId) {
      return;
    }
    next.btn.disabled = true;
    next.note.textContent = "";
    slAuth.apiFetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ shop_id: nextShopId })
    })
      .then(function (res) {
        if (res.status !== 201) {
          throw new Error(nextRoundMessage(res.status));
        }
        return res.json();
      })
      .then(function (session) {
        window.location.href = "/interview/" + encodeURIComponent(session.session_id);
      })
      .catch(function (err) {
        var msg = err && err.message ? err.message : "";
        if (!msg || msg.indexOf("Failed to fetch") !== -1) {
          msg = "서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.";
        }
        next.note.textContent = msg;
        next.btn.disabled = false;
      });
  });

  function area(axis) {
    return document.querySelector('textarea[data-axis="' + axis + '"]');
  }

  function evidenceList(axis) {
    return document.querySelector('ul[data-evidence-axis="' + axis + '"]');
  }

  function clearChildren(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
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

  function showMessage(msg) {
    el.message.textContent = msg;
    el.message.hidden = false;
  }

  function networkMessage(err) {
    var msg = err && err.message ? err.message : "";
    if (!msg || msg.indexOf("Failed to fetch") !== -1) {
      return "서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.";
    }
    return msg;
  }

  function gateItem(ok, text) {
    var li = document.createElement("li");
    li.className = ok ? "ok" : "no";
    var ic = document.createElement("span");
    ic.className = "ic " + (ok ? "ok" : "no");
    ic.setAttribute("aria-hidden", "true");
    ic.textContent = ok ? "✓" : "!";
    var t = document.createElement("span");
    t.textContent = text;
    li.appendChild(ic);
    li.appendChild(t);
    return li;
  }

  function renderList(target, items, okText) {
    clearChildren(target);
    if (!items || !items.length) {
      target.appendChild(gateItem(true, okText));
      return;
    }
    items.forEach(function (text) {
      target.appendChild(gateItem(false, text));
    });
  }

  function renderWarnings(list) {
    renderList(el.warnings, list, "문제 없음");
  }

  function axisLabel(axis) {
    return AXIS_LABELS[axis] || axis;
  }

  // Quality-gate reason codes in plain Korean.
  function reasonText(code) {
    var parts = String(code).split(":");
    if (parts[0] === "warnings") {
      return "가려지지 않은 식별 정보가 있는지 확인이 필요해요 (가림 처리 확인 목록)";
    }
    if (parts[0] === "missing_axis") {
      return "'" + axisLabel(parts[1]) + "' 항목이 비어 있어요";
    }
    if (parts[0] === "low_support") {
      var ratio = parseFloat(parts[2]);
      var shown = isNaN(ratio) ? parts[2] : "근거 있는 문장 " + Math.round(ratio * 100) + "%";
      return "근거가 부족한 문장이 있어요 (" + axisLabel(parts[1]) + " " + shown + ")";
    }
    if (code === "consent_missing:publication") {
      return "공개 동의가 없어요";
    }
    if (code === "owner_approval_missing") {
      return "사장님 승인이 아직 없어요 (2단계 '사장님 확인')";
    }
    return String(code);
  }

  // Quality reasons come from the server (card.quality.reasons or a 409 gate body); the screen only shows them.
  function renderReasons(reasons) {
    currentReasons = (reasons || []).slice();
    renderList(el.reasons, currentReasons.map(reasonText), "공개 기준을 모두 채웠어요");
    if (currentCard) {
      renderFix(currentCard);
      renderSteps(currentCard);
    }
    updateGate();
  }

  function shorten(text, n) {
    var s = String(text || "");
    return s.length > n ? s.slice(0, n) + "…" : s;
  }

  // "고칠 곳": failed quality reasons plus sentences without support. Owner approval lives in step 2, not here.
  function fixItems(card) {
    var items = [];
    var covered = {};
    currentReasons.forEach(function (code) {
      var parts = String(code).split(":");
      if (code === "owner_approval_missing") {
        return;
      }
      if (parts[0] === "warnings") {
        items.push({ text: reasonText(code), act: "확인", pane: "publish", target: el.warningsBox });
      } else if (parts[0] === "missing_axis") {
        items.push({ text: reasonText(code), act: "채우기", axis: parts[1] });
        covered[parts[1]] = true;
      } else if (parts[0] === "low_support") {
        items.push({ text: reasonText(code), act: "문장 보기", axis: parts[1] });
        covered[parts[1]] = true;
      } else if (code === "consent_missing:publication" && card.shop_id) {
        items.push({
          text: reasonText(code), act: "동의 받기",
          href: "/shops/" + encodeURIComponent(card.shop_id) + "/consent"
        });
      } else {
        items.push({ text: reasonText(code), act: "보기", pane: "publish", target: el.reasons });
      }
    });
    AXES.forEach(function (axis) {
      if (covered[axis] || !(card.sections && card.sections[axis])) {
        return;
      }
      ((card.evidence && card.evidence[axis]) || []).forEach(function (ev) {
        if (ev.source !== "reviewer" && !ev.supported) {
          items.push({
            text: axisLabel(axis) + " · 근거 없는 문장 '" + shorten(ev.claim, 28) + "'",
            act: "문장 보기", axis: axis
          });
        }
      });
    });
    return items;
  }

  function renderFix(card) {
    clearChildren(el.fixlist);
    if (card.status === "published") {
      return;
    }
    var items = fixItems(card);
    if (!items.length) {
      var okP = document.createElement("p");
      okP.className = "fix-ok";
      okP.textContent = "고칠 곳이 없어요. 사장님 확인으로 넘어가세요.";
      el.fixlist.appendChild(okP);
      return;
    }
    var head = document.createElement("p");
    head.className = "eyebrow";
    head.textContent = "고칠 곳 " + items.length;
    el.fixlist.appendChild(head);
    items.forEach(function (item) {
      var b = document.createElement("button");
      b.type = "button";
      var t = document.createElement("span");
      t.textContent = item.text;
      var a = document.createElement("span");
      a.textContent = item.act + " →";
      b.appendChild(t);
      b.appendChild(a);
      b.addEventListener("click", function () {
        fixAction(item);
      });
      el.fixlist.appendChild(b);
    });
  }

  function flashTarget(target) {
    var m = motion();
    if (m && m.flash) {
      m.flash(target, true);
    } else if (target.scrollIntoView) {
      target.scrollIntoView({ block: "start" });
    }
  }

  function expandAxis(axis) {
    var head = document.querySelector("#rv-axis-" + axis + " .axis-h");
    if (head && head.getAttribute("aria-expanded") !== "true") {
      toggleAxis(head, true);
    }
  }

  function goToAxis(axis) {
    setTab("edit", false);
    expandAxis(axis);
    var block = document.getElementById("rv-axis-" + axis);
    if (!block) {
      return;
    }
    flashTarget(block);
    area(axis).focus({ preventScroll: true });
  }

  function fixAction(item) {
    if (item.href) {
      window.location.href = item.href;
      return;
    }
    if (item.axis) {
      goToAxis(item.axis);
      return;
    }
    setTab(item.pane || "publish", false);
    if (item.target) {
      flashTarget(item.target);
    }
  }

  // Bottom bar: "공개까지 N가지 남았어요" where N is the number of server quality reasons.
  function updateGate() {
    var t = el.gateText;
    var b = el.gateGo;
    if (!currentCard) {
      return;
    }
    if (currentCard.status === "published") {
      t.textContent = "공개 중이에요. 고치려면 먼저 공개를 취소해 주세요.";
      b.hidden = true;
      return;
    }
    var n = currentReasons.length;
    if (n) {
      t.textContent = "공개까지 " + n + "가지 남았어요";
      b.textContent = "보기";
      b.hidden = false;
    } else {
      t.textContent = "공개 기준을 모두 채웠어요";
      b.textContent = "공개로";
      b.hidden = currentTab === "publish";
    }
  }

  el.gateGo.addEventListener("click", function () {
    if (!currentCard) {
      return;
    }
    var items = fixItems(currentCard);
    if (items.length) {
      fixAction(items[0]);
      return;
    }
    if (currentReasons.indexOf("owner_approval_missing") !== -1) {
      setTab("owner", true);
      return;
    }
    setTab("publish", true);
  });

  function stepMark(tab, done, n) {
    var mark = el.steps.querySelector('[data-tab="' + tab + '"] .n');
    clearChildren(mark);
    if (done) {
      var dot = document.createElement("span");
      dot.className = "dotok";
      mark.appendChild(dot);
    } else {
      mark.textContent = n;
    }
  }

  function renderSteps(card) {
    stepMark("edit", fixItems(card).length === 0, "1");
    stepMark("owner", !!(card.owner_decision && card.owner_decision.decision === "approve"), "2");
    stepMark("publish", card.status === "published", "3");
  }

  function isWide() {
    return !!(wide && wide.matches);
  }

  // Phones show one step at a time; wide screens show all three and the tab scrolls to its step.
  function setTab(tab, scroll) {
    currentTab = tab;
    var idx = TABS.indexOf(tab);
    el.steps.setAttribute("data-at", String(idx));
    Array.prototype.forEach.call(el.steps.querySelectorAll("[role=tab]"), function (b) {
      var on = b.getAttribute("data-tab") === tab;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    });
    TABS.forEach(function (name) {
      var pane = document.getElementById("rv-pane-" + name);
      var on = name === tab;
      var wasOff = pane.classList.contains("is-off");
      pane.classList.toggle("is-off", !on);
      var m = motion();
      if (on && wasOff && scroll && !isWide() && window.gsap && m && !m.reduced) {
        window.gsap.fromTo(pane, { opacity: 0, x: 16 },
          { opacity: 1, x: 0, duration: 0.35, ease: "power2.out", clearProps: "opacity,transform" });
      }
    });
    if (scroll && !isWide() && el.steps.getBoundingClientRect().top < 0 && el.steps.scrollIntoView) {
      el.steps.scrollIntoView({ block: "start" });
    }
    if (scroll && isWide()) {
      var target = document.getElementById("rv-pane-" + tab);
      if (target.scrollIntoView) {
        target.scrollIntoView({ block: "start", behavior: motion() && motion().reduced ? "auto" : "smooth" });
      }
    }
    updateGate();
  }

  Array.prototype.forEach.call(el.steps.querySelectorAll("[role=tab]"), function (b) {
    b.addEventListener("click", function () {
      setTab(b.getAttribute("data-tab"), true);
    });
    b.addEventListener("keydown", function (ev) {
      var idx = TABS.indexOf(b.getAttribute("data-tab"));
      var next = null;
      if (ev.key === "ArrowRight") {
        next = TABS[(idx + 1) % TABS.length];
      } else if (ev.key === "ArrowLeft") {
        next = TABS[(idx + TABS.length - 1) % TABS.length];
      } else if (ev.key === "Home") {
        next = TABS[0];
      } else if (ev.key === "End") {
        next = TABS[TABS.length - 1];
      }
      if (next) {
        ev.preventDefault();
        setTab(next, true);
        el.steps.querySelector('[data-tab="' + next + '"]').focus();
      }
    });
  });

  function toggleAxis(head, open) {
    var body = document.getElementById(head.getAttribute("aria-controls"));
    head.setAttribute("aria-expanded", open ? "true" : "false");
    body.hidden = !open;
  }

  Array.prototype.forEach.call(document.querySelectorAll(".axis-h"), function (head) {
    head.addEventListener("click", function () {
      toggleAxis(head, head.getAttribute("aria-expanded") !== "true");
    });
  });

  function supportCounts(card) {
    var supported = 0;
    var total = 0;
    AXES.forEach(function (axis) {
      if (!(card.sections && card.sections[axis])) {
        return;
      }
      ((card.evidence && card.evidence[axis]) || []).forEach(function (ev) {
        if (ev.source === "reviewer") {
          return;
        }
        total += 1;
        if (ev.supported) {
          supported += 1;
        }
      });
    });
    return { supported: supported, total: total };
  }

  function highlightSource(answerId) {
    var lit = el.sources.querySelectorAll("li.highlight");
    Array.prototype.forEach.call(lit, function (li) {
      li.classList.remove("highlight");
    });
    if (answerId === null || answerId === undefined) {
      return;
    }
    var target = el.sources.querySelector('li[data-answer-id="' + answerId + '"]');
    if (!target) {
      if (sourcesLoaded) {
        focusNote("근거 답변을 아래 목록에서 찾지 못했어요.");
      }
      return;
    }
    target.classList.add("highlight");
    if (target.scrollIntoView) {
      target.scrollIntoView({ block: "nearest" });
    }
  }

  function focusNote(text, cls) {
    var p = document.createElement("p");
    p.className = cls || "hint";
    p.textContent = text;
    el.srcFocus.appendChild(p);
    el.srcFocus.hidden = false;
    return p;
  }

  // Evidence chip → bottom sheet with the original answers; the cited answer is highlighted.
  function openSource(ev, axis) {
    clearChildren(el.srcFocus);
    el.srcFocus.hidden = true;
    pendingAnswerId = null;
    if (!ev) {
      el.srcTitle.textContent = "원 답변 (가림 처리됨)";
    } else if (ev.source === "reviewer") {
      el.srcTitle.textContent = "검수자 작성 문장";
      focusNote(ev.claim, "quote");
      focusNote("검수자가 직접 쓴 문장이라 원 답변 근거가 없어요. 사장님 답변과 어긋나지 않는지 한 번 더 확인해 주세요.");
    } else if (!ev.supported) {
      el.srcTitle.textContent = "근거를 찾지 못한 문장";
      focusNote(ev.claim, "quote");
      focusNote("이 문장은 원 답변에서 근거를 찾지 못했어요. 문장을 고치거나 지워 주세요.");
      var fix = document.createElement("button");
      fix.type = "button";
      fix.className = "btn sec";
      fix.textContent = "이 문장 고치러 가기";
      fix.addEventListener("click", function () {
        closeSheet(el.sheetSource);
        goToAxis(axis);
      });
      el.srcFocus.appendChild(fix);
    } else {
      el.srcTitle.textContent = "원 답변 (가림 처리됨)";
      focusNote("카드 문장 · " + ev.claim);
      if (ev.quote) {
        focusNote(ev.quote, "quote");
      }
      pendingAnswerId = ev.answer_id;
    }
    openSheet(el.sheetSource);
    highlightSource(pendingAnswerId);
  }

  el.support.addEventListener("click", function () {
    openSource(null, null);
  });

  function axisRatio(card, axis) {
    var ok = 0;
    var all = 0;
    if (card.sections && card.sections[axis]) {
      ((card.evidence && card.evidence[axis]) || []).forEach(function (ev) {
        if (ev.source === "reviewer") {
          return;
        }
        all += 1;
        if (ev.supported) {
          ok += 1;
        }
      });
    }
    return { ok: ok, all: all };
  }

  function renderEvidence(card) {
    AXES.forEach(function (axis) {
      var list = evidenceList(axis);
      clearChildren(list);
      var r = axisRatio(card, axis);
      var ratio = document.querySelector('[data-axis-ratio="' + axis + '"]');
      ratio.textContent = "근거 " + r.ok + "/" + r.all + (r.ok < r.all ? " · 확인 필요" : "");
      ratio.classList.toggle("need", r.ok < r.all);
      var items = (card.sections && card.sections[axis]) ? ((card.evidence && card.evidence[axis]) || []) : [];
      items.forEach(function (ev) {
        var li = document.createElement("li");
        li.className = "evidence-item";
        var claim = document.createElement("span");
        claim.className = "evidence-claim";
        claim.textContent = ev.claim;
        var badge = document.createElement("button");
        badge.type = "button";
        badge.setAttribute("aria-haspopup", "dialog");
        if (ev.source === "reviewer") {
          badge.className = "chip mine evidence-badge badge-reviewer";
          badge.textContent = "검수자 작성";
          badge.title = "검수자가 직접 쓴 문장이라 원 답변 근거가 없어요";
        } else {
          badge.className = "chip " + (ev.supported ? "ok" : "none") + " evidence-badge " +
            (ev.supported ? "supported" : "unsupported");
          badge.textContent = ev.supported ? "근거 있음" : "근거 없음";
          if (ev.answer_id !== null && ev.answer_id !== undefined) {
            badge.setAttribute("data-answer-id", String(ev.answer_id));
          }
          if (ev.quote) {
            badge.title = ev.quote;
          }
        }
        badge.addEventListener("click", function () {
          openSource(ev, axis);
        });
        li.appendChild(claim);
        li.appendChild(badge);
        list.appendChild(li);
      });
    });
  }

  function renderSources(answers) {
    clearChildren(el.sources);
    if (!answers.length) {
      el.sourcesEmpty.textContent = "이 가게의 답변이 아직 없어요.";
      el.sourcesEmpty.hidden = false;
      return;
    }
    el.sourcesEmpty.hidden = true;
    answers.forEach(function (a) {
      var li = document.createElement("li");
      li.className = "source-item";
      li.setAttribute("data-answer-id", a.answer_id === null ? "" : String(a.answer_id));
      var meta = document.createElement("p");
      meta.className = "source-meta";
      meta.textContent = a.session_number + "회차 · " + axisLabel(a.axis);
      var q = document.createElement("p");
      q.className = "source-question";
      q.textContent = a.question;
      var ans = document.createElement("p");
      ans.className = "source-answer";
      ans.textContent = a.answer_masked;
      li.appendChild(meta);
      li.appendChild(q);
      li.appendChild(ans);
      el.sources.appendChild(li);
    });
  }

  function loadSources() {
    slAuth.apiFetch(apiUrl + "/sources")
      .then(function (res) {
        if (!res.ok) {
          throw new Error("원 답변을 불러오지 못했습니다. (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (body) {
        sourcesLoaded = true;
        renderSources((body && body.answers) || []);
        if (!el.sheetSource.hidden) {
          highlightSource(pendingAnswerId);
        }
      })
      .catch(function (err) {
        el.sourcesEmpty.textContent = networkMessage(err);
        el.sourcesEmpty.hidden = false;
      });
  }

  function allowed(to) {
    return !!currentCard && (TRANSITIONS[currentCard.status] || []).indexOf(to) !== -1;
  }

  function applyButtons() {
    var published = !!currentCard && currentCard.status === "published";
    el.save.disabled = !currentCard || published;
    el.publish.disabled = !allowed("published");
    el.hold.disabled = !allowed("on_hold");
    el.reject.disabled = !allowed("rejected");
    el.regenerate.disabled = !currentCard || published;
    el.unpublish.hidden = !published;
    el.unpublish.disabled = !allowed("unpublished");
    AXES.forEach(function (axis) {
      area(axis).readOnly = published;
    });
  }

  function renderStatus(card) {
    if (card.status === "published") {
      el.linkA.href = publicUrl;
      el.link.hidden = false;
      setupNextActions(card);
    } else {
      el.link.hidden = true;
      next.box.hidden = true;
    }
    el.publish.hidden = card.status === "published";
  }

  function flagList(card) {
    return (card.quality && card.quality.flags) || [];
  }

  function reviewerCount(card) {
    var n = 0;
    flagList(card).forEach(function (f) {
      var parts = String(f).split(":");
      if (parts[0] === "reviewer_sentences") {
        n = parseInt(parts[1], 10) || 0;
      }
    });
    return n;
  }

  // Which AI wrote the draft. A fallback or quota draft is never shown silently.
  function renderLlm(card) {
    var flags = flagList(card);
    var provider = card.llm_provider;
    var badge = "";
    var warning = "";
    if (provider === "quota" || flags.indexOf("llm_quota_exceeded") !== -1) {
      badge = "AI 한도 초과";
      warning = "이번 달 AI 사용량을 넘어 임시 초안입니다. 원 답변을 보며 문장을 직접 다듬어 주세요.";
    } else if (provider === "fallback" || flags.indexOf("llm_fallback") !== -1) {
      badge = "AI 연결 실패 (임시 초안)";
      warning = "AI 연결 실패로 임시 초안입니다. 원 답변을 보며 문장을 직접 다듬거나 '답변으로 다시 쓰기'를 눌러 주세요.";
    } else if (provider === "fake") {
      badge = "AI 초안 없음 (가짜 초안)";
    } else if (provider) {
      badge = "AI 초안: " + provider;
    }
    el.llmBadge.textContent = badge;
    el.llmBadge.hidden = !badge;
    el.llmBadge.className = "llm-badge" + (warning ? " llm-warn" : "");
    el.llmWarning.textContent = warning;
    el.llmWarning.hidden = !warning;
  }

  function formatTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) {
      return String(iso || "");
    }
    return d.toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
  }

  function renderOwnerDecision(decision) {
    var text = "사장님 검토 전";
    var kind = "none";
    if (decision && decision.decision === "approve") {
      text = "사장님 승인";
      kind = "approve";
    } else if (decision && decision.decision === "request_changes") {
      text = "사장님 수정 요청: " + (decision.comment || "(남긴 말 없음)");
      kind = "request_changes";
    }
    el.ownerDecision.textContent = text;
    el.ownerDecision.className = "owner-decision decision-" + kind;
    if (decision && decision.created_at) {
      el.ownerDecisionMeta.textContent = "답한 시각 " + formatTime(decision.created_at);
      el.ownerDecisionMeta.hidden = false;
    } else {
      el.ownerDecisionMeta.hidden = true;
    }
  }

  function ownerLinkMessage(status, detail) {
    if (status === 422) {
      return "휴대전화 번호를 확인해 주세요. 010으로 시작하는 번호만 받을 수 있어요.";
    }
    if (status === 404) {
      return "카드를 찾지 못했습니다.";
    }
    if (status === 502 || detail === "sms_failed") {
      return "문자를 보내지 못했습니다. 번호를 비우고 링크만 만들어 직접 전해 주세요.";
    }
    if (status === 503 || detail === "owner_links_unavailable") {
      return "지금은 사장님 확인 링크를 만들 수 없습니다. 운영 담당자에게 알려 주세요.";
    }
    return "링크를 만들지 못했습니다. (" + status + ")";
  }

  function showOwnerLink(body) {
    el.ownerLinkUrl.textContent = body.url;
    el.ownerLinkExpires.textContent = body.expires_at ? "링크 만료 " + formatTime(body.expires_at) : "";
    if (body.delivery === "sms") {
      el.ownerLinkNote.textContent = "사장님 휴대전화로 링크를 보냈어요.";
      el.ownerLinkCopy.hidden = true;
    } else {
      el.ownerLinkNote.textContent = "문자는 보내지 않았어요. 아래 링크를 복사해 사장님께 직접 전해 주세요.";
      el.ownerLinkCopy.hidden = false;
    }
    el.ownerLinkCopy.textContent = "링크 복사";
    el.ownerLinkResult.hidden = false;
  }

  var LINK_STATE_LABELS = { active: "유효", revoked: "취소됨", expired: "만료" };

  function setLinkState(li, state) {
    var label = li.querySelector(".owner-link-state");
    label.textContent = LINK_STATE_LABELS[state] || state;
    label.className = "owner-link-state state-" + state;
    li.setAttribute("data-state", state);
    li.querySelector(".owner-link-revoke").hidden = state !== "active";
  }

  function showLinksError(msg) {
    el.ownerLinksError.textContent = msg;
    el.ownerLinksError.hidden = false;
  }

  function revokeLink(li, tokenId, button) {
    el.ownerLinksError.hidden = true;
    button.disabled = true;
    slAuth.apiFetch(apiUrl + "/owner-links/" + encodeURIComponent(tokenId), { method: "DELETE" })
      .then(function (res) {
        if (res.status === 204 || res.status === 409) {
          setLinkState(li, "revoked");
          return;
        }
        if (res.status === 404) {
          throw new Error("링크를 찾지 못했습니다.");
        }
        throw new Error("링크를 취소하지 못했습니다. (" + res.status + ")");
      })
      .catch(function (err) {
        showLinksError(networkMessage(err));
      })
      .then(function () {
        button.disabled = false;
      });
  }

  function renderLinkItem(link) {
    var li = document.createElement("li");
    li.setAttribute("data-token-id", link.token_id);

    var state = document.createElement("span");
    state.className = "owner-link-state";

    var meta = document.createElement("span");
    meta.className = "hint";
    meta.textContent = "만든 시각 " + formatTime(link.issued_at) + " · 만료 " + formatTime(link.expires_at);

    var button = document.createElement("button");
    button.type = "button";
    button.className = "owner-link-revoke";
    button.textContent = "링크 취소";
    button.addEventListener("click", function () {
      revokeLink(li, link.token_id, button);
    });

    li.appendChild(state);
    li.appendChild(meta);
    li.appendChild(button);
    setLinkState(li, link.state);
    return li;
  }

  function renderLinks(links) {
    while (el.ownerLinksList.firstChild) {
      el.ownerLinksList.removeChild(el.ownerLinksList.firstChild);
    }
    links.forEach(function (link) {
      el.ownerLinksList.appendChild(renderLinkItem(link));
    });
    el.ownerLinksEmpty.hidden = links.length !== 0;
  }

  function loadLinks() {
    el.ownerLinksError.hidden = true;
    return slAuth.apiFetch(apiUrl + "/owner-links")
      .then(function (res) {
        if (!res.ok) {
          throw new Error("보낸 링크 목록을 불러오지 못했습니다. (" + res.status + ")");
        }
        return res.json();
      })
      .then(renderLinks)
      .catch(function (err) {
        showLinksError(networkMessage(err));
      });
  }

  el.ownerLinkBtn.addEventListener("click", function () {
    var phone = el.ownerPhone.value.trim();
    el.ownerLinkError.hidden = true;
    el.ownerLinkResult.hidden = true;
    el.ownerLinkBtn.disabled = true;
    slAuth.apiFetch(apiUrl + "/owner-link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone: phone || null })
    })
      .then(function (res) {
        if (res.status === 201) {
          return res.json();
        }
        return res.json().catch(function () { return null; }).then(function (body) {
          throw new Error(ownerLinkMessage(res.status, body ? body.detail : null));
        });
      })
      .then(function (body) {
        showOwnerLink(body);
        return loadLinks();
      })
      .catch(function (err) {
        el.ownerLinkError.textContent = networkMessage(err);
        el.ownerLinkError.hidden = false;
      })
      .then(function () {
        el.ownerLinkBtn.disabled = false;
      });
  });

  function selectLinkText() {
    var range = document.createRange();
    range.selectNodeContents(el.ownerLinkUrl);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  el.ownerLinkCopy.addEventListener("click", function () {
    var url = el.ownerLinkUrl.textContent;
    var done = function () { el.ownerLinkCopy.textContent = "복사했어요"; };
    var manual = function () {
      selectLinkText();
      el.ownerLinkCopy.textContent = "선택했어요. 직접 복사해 주세요";
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(done, manual);
    } else {
      manual();
    }
  });

  function render(card) {
    currentCard = card;
    var t = card.tags;
    var status = STATUS_LABELS[card.status] || card.status;
    el.meta.textContent = status + " · " + slLabels.tradeLabel(t.trade) + " · " +
      (AREA_LABELS[t.area_type] || t.area_type) + " · " + t.years + "년 운영 · " +
      t.closed_year + "년 폐업 · 인터뷰 " + card.sessions + "회 · 버전 " + card.version;
    el.status.textContent = status;
    el.status.className = "pill " + (STATUS_PILLS[card.status] || "draft") + " status-" + card.status;
    var counts = supportCounts(card);
    var written = reviewerCount(card);
    el.support.textContent = "근거 있는 문장 " + counts.supported + "/" + counts.total +
      (written ? " · 검수자 작성 " + written : "") + " · 원 답변 보기";
    AXES.forEach(function (axis) {
      area(axis).value = (card.sections && card.sections[axis]) || "";
    });
    if (card.note) {
      el.currentNote.textContent = "지난 메모: " + card.note;
      el.currentNote.hidden = false;
    } else {
      el.currentNote.hidden = true;
    }
    renderEvidence(card);
    renderWarnings(card.warnings);
    renderReasons(card.quality ? card.quality.reasons : []);
    renderStatus(card);
    renderLlm(card);
    renderOwnerDecision(card.owner_decision);
    applyButtons();
  }

  function firstRender(card) {
    render(card);
    setTab(card.status === "published" ? "publish" : "edit", false);
    if (window.slShell && slShell.coach) {
      var items = fixItems(card);
      slShell.coach(items.length ? el.fixlist.querySelector("button") : el.steps.querySelector('[data-tab="owner"]'));
    }
  }

  function collect() {
    var sections = {};
    AXES.forEach(function (axis) {
      sections[axis] = area(axis).value;
    });
    return sections;
  }

  function setBusy(busy) {
    if (!busy) {
      applyButtons();
      return;
    }
    [el.save, el.publish, el.hold, el.reject, el.regenerate, el.unpublish].forEach(function (b) {
      b.disabled = true;
    });
  }

  // 409 bodies (bad transition or failed gate) turned into a message; gate reasons also refresh the checklist.
  function conflictMessage(body, to) {
    var detail = body ? body.detail : null;
    if (detail && detail.code === "gate_failed") {
      renderReasons(detail.reasons);
      setTab("publish", true);
      return "공개 기준을 채우지 못해 공개하지 않았어요. '공개 전 확인' 목록을 고친 뒤 다시 시도해 주세요.";
    }
    if (detail && detail.code === "invalid_transition") {
      return "지금 상태(" + (STATUS_LABELS[detail.from] || detail.from) + ")에서는 '" +
        (STATUS_LABELS[detail.to] || detail.to) + "'(으)로 바꿀 수 없습니다.";
    }
    if (detail === "card is published") {
      return "공개된 카드는 고칠 수 없습니다. 먼저 공개를 취소해 주세요.";
    }
    return (STATUS_LABELS[to] || "요청한 작업") + " 처리를 하지 못했습니다. (409)";
  }

  function readCard(res, to, failText) {
    if (res.status === 409) {
      return res.json().catch(function () { return null; }).then(function (body) {
        throw new Error(conflictMessage(body, to));
      });
    }
    if (res.status === 404) {
      throw new Error("카드를 찾지 못했습니다.");
    }
    if (!res.ok) {
      throw new Error(failText + " (" + res.status + ")");
    }
    return res.json();
  }

  function save() {
    return slAuth.apiFetch(apiUrl, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sections: collect() })
    }).then(function (res) {
      if (res.status === 422) {
        throw new Error("저장하지 못했습니다. 각 항목은 5000자 이내로 적어 주세요.");
      }
      return readCard(res, null, "저장하지 못했습니다.");
    }).then(function (card) {
      render(card);
      return card;
    });
  }

  function changeStatus(to) {
    var note = el.note.value.trim();
    return slAuth.apiFetch(apiUrl + "/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: to, note: note || null })
    }).then(function (res) {
      return readCard(res, to, "상태를 바꾸지 못했습니다.");
    }).then(function (card) {
      el.note.value = "";
      render(card);
      return card;
    });
  }

  function load() {
    slAuth.apiFetch(apiUrl)
      .then(function (res) {
        if (res.status === 404) {
          throw new Error("카드를 찾지 못했습니다.");
        }
        if (!res.ok) {
          throw new Error("카드를 불러오지 못했습니다. (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (card) {
        firstRender(card);
        loadSources();
        loadLinks();
      })
      .catch(function (err) {
        showError(networkMessage(err));
      });
  }

  function run(task, doneText) {
    closeSheet(el.sheetMore);
    clearError();
    el.message.hidden = true;
    setBusy(true);
    task()
      .then(function (card) {
        if (doneText) {
          showMessage(typeof doneText === "function" ? doneText(card) : doneText);
        }
      })
      .catch(function (err) {
        showError(networkMessage(err));
      })
      .then(function () {
        setBusy(false);
      });
  }

  el.save.addEventListener("click", function () {
    run(save, "저장했어요.");
  });

  ["hold", "reject"].forEach(function (key) {
    var to = key === "hold" ? "on_hold" : "rejected";
    el[key].addEventListener("click", function () {
      run(function () {
        return save().then(function () { return changeStatus(to); });
      }, DONE_MESSAGES[to]);
    });
  });

  el.unpublish.addEventListener("click", function () {
    run(function () { return changeStatus("unpublished"); }, DONE_MESSAGES.unpublished);
  });

  el.regenerate.addEventListener("click", function () {
    run(function () {
      return slAuth.apiFetch(apiUrl + "/regenerate", { method: "POST" })
        .then(function (res) {
          return readCard(res, "draft", "다시 쓰지 못했습니다.");
        })
        .then(function (card) {
          render(card);
          return card;
        });
    }, "원 답변으로 초안을 다시 썼어요. 문장별 근거를 다시 확인해 주세요.");
  });

  // 公開 stamp, then the public card. Navigation never waits on the animation for more than 2.2s.
  function stampThenGo(href) {
    var gone = false;
    var leave = function () {
      if (gone) {
        return;
      }
      gone = true;
      window.location.href = href;
    };
    var m = motion();
    if (m && m.stamp && el.stamp) {
      m.stamp(el.stamp, "公開", leave);
      window.setTimeout(leave, 2200);
    } else {
      leave();
    }
  }

  el.more.addEventListener("click", function () {
    openSheet(el.sheetMore);
  });

  el.publish.addEventListener("click", function () {
    clearError();
    setBusy(true);
    save()
      .then(function () {
        return changeStatus("published");
      })
      .then(function () {
        showMessage("공개했어요. 카드 화면으로 이동해요.");
        stampThenGo(publicUrl);
      })
      .catch(function (err) {
        el.message.hidden = true;
        showError(networkMessage(err));
        setBusy(false);
      });
  });

  if (window.GYMotion) {
    window.GYMotion.enterPage();
  }
  load();
})();
