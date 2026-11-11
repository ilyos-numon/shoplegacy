(function () {
  "use strict";

  var AXES = ["location", "suppliers", "menu", "pricing", "customers", "failures"];
  var AREA_LABELS = { residential: "주택가", station: "역세권", office: "오피스가", other: "기타 지역" };

  var parts = window.location.pathname.split("/").filter(function (s) { return s.length > 0; });
  var cardId = decodeURIComponent(parts[parts.length - 1] || "");

  var tagsEl = document.getElementById("tags");
  var errorEl = document.getElementById("error");

  var REASON_NOTES = {
    limit: "예정 회차를 모두 마쳤어요.",
    exhausted: "질문을 모두 마쳤어요."
  };

  var next = {
    box: document.getElementById("next-actions"),
    btn: document.getElementById("next-round-btn"),
    note: document.getElementById("next-round-note")
  };
  var nextShopId = null;

  function nextRoundMessage(status) {
    if (status === 409) {
      return "이 가게의 인터뷰는 더 진행할 수 없어요. 예정된 횟수를 모두 마쳤거나 질문이 남지 않았어요.";
    }
    if (status === 404) {
      return "가게 정보를 찾지 못했어요.";
    }
    return "다음 회차를 시작하지 못했어요. 잠시 뒤 다시 시도해 주세요. (" + status + ")";
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
    // Operator actions need a login; anonymous visitors only read the card.
    slAuth.getToken().then(function (token) {
      if (!token) {
        return;
      }
      nextShopId = card.shop_id;
      next.box.hidden = false;
      loadProgress(card.shop_id);
    });
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
          msg = "서버에 연결하지 못했어요. 잠시 뒤 다시 시도해 주세요.";
        }
        next.note.textContent = msg;
        next.btn.disabled = false;
      });
  });

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.hidden = false;
  }

  // Public badge: supported/total over answer-checked claims of non-empty axes. Sentences
  // the reviewer wrote are counted apart and shown in their own marker, never as evidence.
  // quality.reasons/gate_ok are not shown (anonymous readers cannot see consents).
  function renderEvidenceBadge(card) {
    var supported = 0;
    var total = 0;
    var reviewerWritten = 0;
    AXES.forEach(function (axis) {
      if (!(card.sections && card.sections[axis])) {
        return;
      }
      ((card.evidence && card.evidence[axis]) || []).forEach(function (ev) {
        if (ev.source === "reviewer") {
          reviewerWritten += 1;
          return;
        }
        total += 1;
        if (ev.supported) {
          supported += 1;
        }
      });
    });
    var badge = document.getElementById("evidence-badge");
    badge.textContent = "근거 " + supported + "/" + total;
    badge.hidden = false;
    var marker = document.getElementById("reviewer-marker");
    if (reviewerWritten > 0) {
      marker.textContent = "검수자 작성 " + reviewerWritten + "문장";
      marker.hidden = false;
    } else {
      marker.textContent = "";
      marker.hidden = true;
    }
  }

  var fb = {
    box: document.getElementById("feedback"),
    up: document.getElementById("feedback-up"),
    down: document.getElementById("feedback-down"),
    counts: document.getElementById("feedback-counts"),
    thanks: document.getElementById("feedback-thanks")
  };

  function renderFeedbackCounts(counts) {
    var up = (counts && counts.up) || 0;
    var down = (counts && counts.down) || 0;
    fb.counts.textContent = "도움됐어요 " + up + " · 아쉬워요 " + down;
  }

  function feedbackNote(msg) {
    fb.thanks.textContent = msg;
    fb.thanks.hidden = false;
  }

  function sendFeedback(vote) {
    fb.up.disabled = true;
    fb.down.disabled = true;
    fetch("/api/cards/" + encodeURIComponent(cardId) + "/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vote: vote })
    })
      .then(function (res) {
        if (res.ok) {
          return res.json().then(function (body) {
            renderFeedbackCounts(body);
            feedbackNote(body.counted
              ? "의견 고마워요."
              : "오늘은 이미 의견을 남겨 주셨어요. 고맙습니다.");
          });
        }
        if (res.status === 404) {
          feedbackNote("이 카드는 더 이상 공개되어 있지 않아 의견을 받을 수 없어요.");
          return;
        }
        if (res.status === 409 || res.status === 429) {
          feedbackNote("오늘은 이미 의견을 남겨 주셨어요. 고맙습니다.");
          return;
        }
        feedbackNote("의견을 보내지 못했어요. 잠시 뒤 다시 시도해 주세요.");
        fb.up.disabled = false;
        fb.down.disabled = false;
      })
      .catch(function () {
        feedbackNote("서버에 연결하지 못했어요. 잠시 뒤 다시 시도해 주세요.");
        fb.up.disabled = false;
        fb.down.disabled = false;
      });
  }

  fb.up.addEventListener("click", function () {
    sendFeedback("up");
  });
  fb.down.addEventListener("click", function () {
    sendFeedback("down");
  });

  function render(card) {
    document.getElementById("card-code").textContent = String(card.card_id).slice(0, 8).toUpperCase();
    var t = card.tags;
    tagsEl.textContent = [
      slLabels.tradeLabel(t.trade),
      AREA_LABELS[t.area_type] || t.area_type,
      t.region,
      t.years + "년 운영",
      t.closed_year + "년 폐업"
    ].filter(function (s) { return s; }).join(" · ");
    AXES.forEach(function (axis) {
      var body = document.querySelector('section[data-axis="' + axis + '"] .body');
      var text = (card.sections && card.sections[axis]) || "";
      body.textContent = text || "이 항목은 아직 이야기가 없어요.";
      if (!text) {
        body.className = "body empty";
      }
    });
    renderEvidenceBadge(card);
    renderFeedbackCounts(card.feedback);
    fb.box.hidden = false;
  }

  if (window.GYMotion) {
    window.GYMotion.enterPage();
  }

  // Public card: plain fetch without a token, so anonymous visitors are never redirected.
  fetch("/api/cards/" + encodeURIComponent(cardId))
    .then(function (res) {
      if (!res.ok) {
        throw new Error("카드를 불러오지 못했어요.");
      }
      return res.json();
    })
    .then(function (card) {
      if (card.status !== "published") {
        throw new Error("아직 공개되지 않은 카드예요.");
      }
      render(card);
      document.getElementById("review-link").href = "/review/" + encodeURIComponent(card.card_id);
      setupNextActions(card);
    })
    .catch(function (err) {
      var msg = err && err.message ? err.message : "";
      if (!msg || msg.indexOf("Failed to fetch") !== -1) {
        msg = "서버에 연결하지 못했어요. 잠시 뒤 다시 시도해 주세요.";
      }
      showError(msg);
    });
})();
