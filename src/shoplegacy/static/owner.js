(function () {
  "use strict";

  var AXES = ["location", "suppliers", "menu", "pricing", "customers", "failures"];
  var AXIS_LABELS = {
    location: "입지",
    suppliers: "거래처",
    menu: "메뉴",
    pricing: "가격",
    customers: "손님",
    failures: "실패"
  };
  var MESSAGES = {
    expired: "링크가 만료됐어요. 담당자에게 다시 요청해 주세요",
    revoked: "담당자가 이 링크를 취소했어요. 담당자에게 다시 요청해 주세요",
    invalid: "링크가 올바르지 않아요. 담당자에게 다시 요청해 주세요",
    notFound: "카드를 찾지 못했어요. 담당자에게 다시 요청해 주세요",
    network: "인터넷 연결을 확인하고 잠시 뒤 다시 열어 주세요",
    chooseFirst: "\"이대로 괜찮아요\" 또는 \"고칠 게 있어요\" 중 하나를 먼저 눌러 주세요",
    tooLong: "하고 싶은 말은 500자까지 적을 수 있어요",
    thanks: "고맙습니다. 사장님 답을 담당자에게 전했어요.",
    thanksApprove: "고맙습니다. \"이대로 괜찮아요\"라고 담당자에게 전했어요.",
    thanksChanges: "고맙습니다. \"고칠 게 있어요\"라고 담당자에게 전했어요. 담당자가 고친 뒤 다시 연락드려요."
  };

  var parts = window.location.pathname.split("/").filter(function (s) { return s.length > 0; });
  var token = decodeURIComponent(parts[parts.length - 1] || "");

  var el = {
    loading: document.getElementById("owner-loading"),
    error: document.getElementById("owner-error"),
    card: document.getElementById("owner-card"),
    trade: document.getElementById("owner-trade"),
    expires: document.getElementById("owner-expires"),
    decide: document.getElementById("owner-decide"),
    approve: document.getElementById("owner-approve"),
    changes: document.getElementById("owner-changes"),
    comment: document.getElementById("owner-comment"),
    commentWrap: document.getElementById("owner-comment-wrap"),
    stamp: document.getElementById("stamp"),
    submit: document.getElementById("owner-submit"),
    result: document.getElementById("owner-result"),
    tts: document.getElementById("tts-btn")
  };

  var state = { choice: "", busy: false, card: null };

  function apiUrl(suffix) {
    return "/api/owner/" + encodeURIComponent(token) + (suffix || "");
  }

  function showError(msg, reason) {
    el.error.textContent = msg;
    el.error.dataset.reason = reason || "";
    el.error.hidden = false;
  }

  function clearError() {
    el.error.textContent = "";
    el.error.dataset.reason = "";
    el.error.hidden = true;
  }

  function showResult(msg) {
    el.result.textContent = msg;
    el.result.hidden = false;
  }

  function linkError(status, detail) {
    if (status === 410 && detail === "link_revoked") {
      return { msg: MESSAGES.revoked, reason: "revoked" };
    }
    if (status === 410) {
      return { msg: MESSAGES.expired, reason: "expired" };
    }
    if (status === 401) {
      return { msg: MESSAGES.invalid, reason: "invalid" };
    }
    if (status === 404) {
      return { msg: MESSAGES.notFound, reason: "notFound" };
    }
    return null;
  }

  function readDetail(res) {
    return res.json()
      .then(function (body) {
        return body && typeof body.detail === "string" ? body.detail : "";
      })
      .catch(function () {
        return "";
      });
  }

  function linkFailure(known) {
    var err = new Error(known.msg);
    err.reason = known.reason;
    return err;
  }

  function formatDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) {
      return "";
    }
    return d.getFullYear() + "년 " + (d.getMonth() + 1) + "월 " + d.getDate() + "일";
  }

  function tradeText(trade) {
    if (window.slLabels && typeof window.slLabels.tradeLabel === "function") {
      return window.slLabels.tradeLabel(trade);
    }
    return trade || "";
  }

  function decisionText(decision) {
    if (decision && decision.decision === "approve") {
      return "이미 \"이대로 괜찮아요\"라고 답해 주셨어요. 고맙습니다.";
    }
    var text = "이미 \"고칠 게 있어요\"라고 답해 주셨어요. 고맙습니다.";
    if (decision && decision.comment) {
      text += " 남기신 말: " + decision.comment;
    }
    return text;
  }

  function renderCard(card) {
    state.card = card;
    el.trade.textContent = card.trade ? "업종: " + tradeText(card.trade) : "";
    AXES.forEach(function (axis) {
      var body = el.card.querySelector('section[data-axis="' + axis + '"] .body');
      var text = (card.sections && card.sections[axis]) || "";
      if (text.trim()) {
        body.textContent = text;
        body.classList.remove("empty");
      } else {
        body.textContent = "아직 적힌 내용이 없어요.";
        body.classList.add("empty");
      }
    });
    var until = card.expires_at ? formatDate(card.expires_at) : "";
    el.expires.textContent = until ? "이 링크는 " + until + "까지 열 수 있어요." : "";
    el.card.hidden = false;
    if (el.tts) {
      el.tts.disabled = false;
    }
    if (card.decision) {
      el.decide.hidden = true;
      showResult(decisionText(card.decision));
    } else {
      el.decide.hidden = false;
    }
  }

  function readAloudText() {
    var card = state.card;
    if (!card) {
      return "";
    }
    var lines = ["가게 이야기를 읽어 드릴게요."];
    AXES.forEach(function (axis) {
      var text = (card.sections && card.sections[axis]) || "";
      if (text.trim()) {
        lines.push(AXIS_LABELS[axis] + ". " + text);
      }
    });
    if (!card.decision) {
      lines.push("괜찮으시면 이대로 괜찮아요 버튼을, 고칠 게 있으면 고칠 게 있어요 버튼을 누르고 보내기를 눌러 주세요.");
    }
    return lines.join("\n");
  }

  function choose(choice) {
    state.choice = choice;
    el.approve.setAttribute("aria-pressed", choice === "approve" ? "true" : "false");
    el.changes.setAttribute("aria-pressed", choice === "request_changes" ? "true" : "false");
    clearError();
    el.commentWrap.hidden = choice !== "request_changes";
    if (choice === "request_changes") {
      el.comment.focus();
    }
  }

  function finish(msg, approved) {
    el.decide.hidden = true;
    showResult(msg);
    if (approved && el.stamp && window.GYMotion) {
      window.GYMotion.stamp(el.stamp, "確認");
    }
  }

  function load() {
    fetch(apiUrl(""), { headers: { Accept: "application/json" } })
      .then(function (res) {
        if (res.status === 410 || res.status === 401 || res.status === 404) {
          return readDetail(res).then(function (detail) {
            throw linkFailure(linkError(res.status, detail));
          });
        }
        if (!res.ok) {
          throw new Error("내용을 불러오지 못했어요. 잠시 뒤 다시 열어 주세요. (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (card) {
        el.loading.hidden = true;
        renderCard(card);
      })
      .catch(function (err) {
        el.loading.hidden = true;
        if (el.tts) {
          el.tts.hidden = true;
        }
        var msg = err && err.message ? err.message : "";
        if (!msg || msg.indexOf("Failed to fetch") !== -1) {
          msg = MESSAGES.network;
        }
        showError(msg, err && err.reason ? err.reason : "");
      });
  }

  el.approve.addEventListener("click", function () {
    choose("approve");
  });

  el.changes.addEventListener("click", function () {
    choose("request_changes");
  });

  el.submit.addEventListener("click", function () {
    if (state.busy) {
      return;
    }
    clearError();
    if (!state.choice) {
      showError(MESSAGES.chooseFirst);
      return;
    }
    var comment = el.comment.value.trim();
    if (comment.length > 500) {
      showError(MESSAGES.tooLong);
      return;
    }
    var payload = { decision: state.choice };
    if (comment) {
      payload.comment = comment;
    }
    state.busy = true;
    el.submit.disabled = true;
    fetch(apiUrl("/decision"), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (res) {
        if (res.status === 409) {
          finish("이미 답을 보내 주셨어요. 고맙습니다.");
          return;
        }
        if (res.status === 410 || res.status === 401 || res.status === 404) {
          el.decide.hidden = true;
          return readDetail(res).then(function (detail) {
            throw linkFailure(linkError(res.status, detail));
          });
        }
        if (res.status === 422) {
          throw new Error("보낸 내용을 확인해 주세요. 하고 싶은 말은 500자까지예요.");
        }
        if (!res.ok) {
          throw new Error("보내지 못했어요. 잠시 뒤 다시 눌러 주세요. (" + res.status + ")");
        }
        finish(state.choice === "approve" ? MESSAGES.thanksApprove
          : state.choice === "request_changes" ? MESSAGES.thanksChanges : MESSAGES.thanks,
        state.choice === "approve");
      })
      .catch(function (err) {
        var msg = err && err.message ? err.message : "";
        if (!msg || msg.indexOf("Failed to fetch") !== -1) {
          msg = MESSAGES.network;
        }
        showError(msg, err && err.reason ? err.reason : "");
      })
      .then(function () {
        state.busy = false;
        el.submit.disabled = false;
      });
  });

  if (window.slA11y) {
    window.slA11y.init();
    if (el.tts) {
      el.tts.addEventListener("click", function () {
        window.slA11y.speak(readAloudText());
      });
    }
  }

  // No enterPage here: owners open this page straight from a link, never from another screen.
  load();
})();
