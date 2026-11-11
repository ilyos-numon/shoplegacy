(function () {
  "use strict";

  var TEXT_ONLY_KEY = "sl_text_only"; // shared with interview.js: "1" = typed answers, no mic
  var KIND_LABELS = {
    recording: "대화 녹음",
    storage: "기록 보관",
    ai_processing: "AI 정리",
    publication: "카드 공개"
  };
  var METHOD_LABELS = { verbal: "말로", written: "서면", link: "링크" };
  var REVOKE_REASON = "사장님 요청으로 철회";

  var match = window.location.pathname.match(/^\/shops\/([^/]+)\/consent\/?$/);
  var shopId = match ? decodeURIComponent(match[1]) : "";
  var apiBase = "/api/shops/" + encodeURIComponent(shopId);

  var form = document.getElementById("consent-form");
  var saveBtn = document.getElementById("consent-save");
  var errorBox = document.getElementById("consent-error");
  var codeEl = document.getElementById("consent-code");
  var tagsEl = document.getElementById("consent-shop-tags");
  var methodBox = document.getElementById("consent-method");
  var table = document.getElementById("consent-table");
  var tableEmpty = document.getElementById("consent-table-empty");
  var boxes = {
    recording: document.getElementById("consent-recording"),
    storage: document.getElementById("consent-retention"),
    ai_processing: document.getElementById("consent-llm"),
    publication: document.getElementById("consent-publish")
  };

  var progress = null; // ShopSummary from /progress
  var active = {}; // kind -> active Consent

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

  function toast(msg) {
    var s = shell();
    if (s && s.toast) {
      s.toast(msg);
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

  function connectMessage(err) {
    var msg = err && err.message ? err.message : "";
    if (!msg || msg.indexOf("Failed to fetch") !== -1) {
      msg = "서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.";
    }
    return msg;
  }

  function postJson(url, body) {
    return slAuth.apiFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }

  function setTextOnly(on) {
    try {
      if (on) {
        window.sessionStorage.setItem(TEXT_ONLY_KEY, "1");
      } else {
        window.sessionStorage.removeItem(TEXT_ONLY_KEY);
      }
    } catch (e) {
      // storage unavailable: interview falls back to its default mode
    }
  }

  // ---- method pills ----
  function method() {
    return methodBox.getAttribute("data-value") || "verbal";
  }

  Array.prototype.forEach.call(methodBox.querySelectorAll("button"), function (b) {
    b.addEventListener("click", function () {
      methodBox.setAttribute("data-value", b.getAttribute("data-value"));
      Array.prototype.forEach.call(methodBox.querySelectorAll("button"), function (x) {
        var on = x === b;
        x.setAttribute("aria-checked", on ? "true" : "false");
      });
    });
  });

  Object.keys(boxes).forEach(function (k) {
    boxes[k].addEventListener("change", clearError);
  });

  // ---- existing consents ----
  function fmtDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) {
      return "";
    }
    return (d.getMonth() + 1) + "월 " + d.getDate() + "일";
  }

  function renderTable(list) {
    while (table.firstChild) {
      table.removeChild(table.firstChild);
    }
    tableEmpty.hidden = list.length !== 0;
    list.forEach(function (c) {
      var li = document.createElement("li");
      li.className = "consent-item" + (c.revoked_at ? " revoked" : "");
      li.setAttribute("data-consent-id", c.consent_id);
      li.setAttribute("data-kind", c.kind);

      var name = document.createElement("b");
      name.textContent = KIND_LABELS[c.kind] || c.kind;
      li.appendChild(name);

      var meta = document.createElement("span");
      meta.className = "hint";
      meta.textContent = c.revoked_at
        ? fmtDate(c.revoked_at) + " 철회함"
        : fmtDate(c.granted_at) + " " + (METHOD_LABELS[c.method] || c.method) + " 받음";
      li.appendChild(meta);

      if (!c.revoked_at) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn quiet revoke-btn";
        btn.textContent = "철회";
        btn.addEventListener("click", function () {
          revoke(c, btn);
        });
        li.appendChild(btn);
      }
      table.appendChild(li);
    });
  }

  function applyActive(list) {
    active = {};
    list.forEach(function (c) {
      if (!c.revoked_at) {
        active[c.kind] = c;
      }
    });
    // Revisit: show what the owner actually agreed to. Required kinds stay on.
    if (list.length) {
      boxes.recording.checked = Boolean(active.recording);
      boxes.publication.checked = Boolean(active.publication);
    }
  }

  function loadConsents() {
    return slAuth.apiFetch(apiBase + "/consents")
      .then(function (res) {
        if (res.status === 404) {
          throw new Error("가게 정보를 찾지 못했어요. 홈에서 다시 골라 주세요.");
        }
        if (!res.ok) {
          throw new Error("받은 동의를 불러오지 못했어요. (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (list) {
        applyActive(list);
        renderTable(list);
      });
  }

  function revoke(c, btn) {
    clearError();
    btn.disabled = true;
    postJson("/api/consents/" + encodeURIComponent(c.consent_id) + "/revoke", { reason: REVOKE_REASON })
      .then(function (res) {
        if (!res.ok && res.status !== 409) {
          throw new Error("동의를 철회하지 못했어요. (" + res.status + ")");
        }
        toast((KIND_LABELS[c.kind] || c.kind) + " 동의를 철회했어요");
        return loadConsents();
      })
      .catch(function (err) {
        showError(connectMessage(err));
        btn.disabled = false;
      });
  }

  // ---- shop summary ----
  function loadProgress() {
    return slAuth.apiFetch(apiBase + "/progress")
      .then(function (res) {
        if (res.status === 404) {
          throw new Error("가게 정보를 찾지 못했어요. 홈에서 다시 골라 주세요.");
        }
        if (!res.ok) {
          throw new Error("가게 정보를 불러오지 못했어요. (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (p) {
        progress = p;
        codeEl.textContent = String(p.shop_id).slice(0, 6).toUpperCase();
        tagsEl.textContent = [
          slLabels.tradeLabel(p.trade),
          (slLabels.area && slLabels.area[p.area_type]) || p.area_type,
          p.years + "년 운영",
          "인터뷰 " + p.sessions_done + "/" + p.planned + "회"
        ].join(" · ");
        if (p.open_session_id) {
          saveBtn.textContent = "동의 기록하고 인터뷰 이어가기";
        } else if (!p.can_continue) {
          saveBtn.textContent = "동의 기록하기";
        }
      });
  }

  // ---- save ----
  function grant(kind) {
    if (active[kind]) {
      return Promise.resolve(true);
    }
    return postJson(apiBase + "/consents", { kind: kind, method: method() })
      .then(function (res) {
        if (res.ok || res.status === 409) {
          return true; // 409: already active (recorded in another tab)
        }
        throw new Error((KIND_LABELS[kind] || kind) + " 동의를 기록하지 못했어요. (" + res.status + ")");
      });
  }

  function startInterview() {
    if (progress && progress.open_session_id) {
      return Promise.resolve(progress.open_session_id);
    }
    return postJson("/api/sessions", { shop_id: shopId })
      .then(function (res) {
        if (res.status === 409) {
          throw new Error("이 가게의 인터뷰는 더 진행할 수 없어요. 예정된 횟수를 모두 마쳤거나 질문이 남지 않았어요.");
        }
        if (!res.ok) {
          throw new Error("인터뷰를 시작하지 못했어요. 잠시 후 다시 시도해 주세요. (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (s) {
        return s.session_id;
      });
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    clearError();
    if (!shopId) {
      showError("가게 정보를 찾지 못했어요. 홈에서 다시 골라 주세요.");
      return;
    }
    if (!boxes.storage.checked || !boxes.ai_processing.checked) {
      showError("기록 보관과 AI 정리에 동의해야 인터뷰를 시작할 수 있어요.");
      return;
    }
    var kinds = Object.keys(boxes).filter(function (k) {
      return boxes[k].checked;
    });
    saveBtn.disabled = true;
    // Sequential: keeps the consent log in a stable order.
    var chain = Promise.resolve();
    kinds.forEach(function (k) {
      chain = chain.then(function () {
        return grant(k);
      });
    });
    chain
      .then(function () {
        setTextOnly(!boxes.recording.checked);
        if (progress && !progress.open_session_id && !progress.can_continue) {
          toast("동의 " + kinds.length + "건을 기록했어요");
          saveBtn.disabled = false;
          return loadConsents();
        }
        return startInterview().then(function (sessionId) {
          go("/interview/" + encodeURIComponent(sessionId));
        });
      })
      .catch(function (err) {
        showError(connectMessage(err));
        saveBtn.disabled = false;
        loadConsents().catch(function () {});
      });
  });

  loadProgress()
    .then(loadConsents)
    .then(function () {
      var s = shell();
      if (s && s.coach) {
        // Wait for the page enter slide so the ring is measured in place.
        window.setTimeout(function () {
          s.coach(saveBtn);
        }, 500);
      }
    })
    .catch(function (err) {
      showError(connectMessage(err));
      saveBtn.disabled = true;
    });

  document.addEventListener("DOMContentLoaded", function () {
    if (window.GYMotion) {
      window.GYMotion.enterPage();
    }
  });
})();
