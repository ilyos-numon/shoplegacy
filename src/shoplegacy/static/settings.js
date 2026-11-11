// Org settings: name (read only), record retention days (owner edits, admin reads)
// and the server-fixed "owner approval before publishing" switch (read only).
(function () {
  "use strict";

  var ORG_KEY = "sl_org_id";
  var MIN_DAYS = 30;

  var el = {
    error: document.getElementById("settings-error"),
    name: document.getElementById("settings-org-name"),
    days: document.getElementById("retention-days"),
    note: document.getElementById("retention-note"),
    save: document.getElementById("retention-save"),
    msg: document.getElementById("retention-msg"),
    approval: document.getElementById("require-approval")
  };

  var isOwner = false;

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

  function apiBase() {
    return "/api/orgs/" + encodeURIComponent(orgId() || "");
  }

  function readJson(res) {
    return res.json().catch(function () {
      return {};
    });
  }

  function fmt(n) {
    return Number(n).toLocaleString("ko-KR");
  }

  function showError(msg) {
    el.error.textContent = msg;
    el.error.hidden = false;
  }

  function showMsg(msg, isError) {
    el.msg.textContent = msg;
    el.msg.className = isError ? "err" : "info";
    el.msg.hidden = false;
  }

  function renderRetention(r) {
    el.days.value = String(r.days);
    el.note.textContent = "지금 기준으로 다음 정리 때 지워질 가게: " + fmt(r.next_purge_count) + "곳";
  }

  function loadSettings() {
    return window.slAuth.apiFetch(apiBase() + "/settings").then(function (res) {
      return readJson(res).then(function (body) {
        if (res.status === 403) {
          showError("기관 설정은 기관 관리자와 소유자만 볼 수 있어요.");
          return false;
        }
        if (!res.ok) {
          showError("기관 설정을 불러오지 못했어요. 잠시 후 다시 시도해 주세요. (" + res.status + ")");
          return false;
        }
        el.name.value = body.name || "";
        el.approval.checked = !!body.require_owner_approval;
        return true;
      });
    });
  }

  function loadRetention() {
    return window.slAuth.apiFetch(apiBase() + "/retention").then(function (res) {
      return readJson(res).then(function (body) {
        if (!res.ok) {
          el.note.textContent = "보관 기간을 불러오지 못했어요. (" + res.status + ")";
          return;
        }
        renderRetention(body);
        el.days.disabled = !isOwner;
        el.save.disabled = !isOwner;
        if (!isOwner) {
          el.note.textContent += " · 기관 소유자만 바꿀 수 있어요.";
        } else {
          var s = shell();
          if (s && s.coach) {
            // Wait for the page enter slide so the ring is measured in place.
            window.setTimeout(function () {
              s.coach(el.save);
            }, 500);
          }
        }
      });
    });
  }

  function save() {
    var days = Number(el.days.value);
    if (!Number.isInteger(days) || days < MIN_DAYS) {
      showMsg("보관 기간은 " + MIN_DAYS + "일 이상으로 입력해 주세요.", true);
      el.days.focus();
      return;
    }
    el.save.disabled = true;
    window.slAuth.apiFetch(apiBase() + "/retention", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days: days })
    }).then(function (res) {
      return readJson(res).then(function (body) {
        if (res.status === 403) {
          showMsg("보관 기간은 기관 소유자만 바꿀 수 있어요.", true);
          return;
        }
        if (res.status === 422) {
          showMsg("보관 기간은 " + MIN_DAYS + "일 이상으로 입력해 주세요.", true);
          return;
        }
        if (!res.ok) {
          showMsg("저장하지 못했어요. 잠시 후 다시 시도해 주세요. (" + res.status + ")", true);
          return;
        }
        renderRetention(body);
        showMsg("보관 기간을 " + fmt(body.days) + "일로 저장했어요.", false);
      });
    }).catch(function () {
      showMsg("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.", true);
    }).then(function () {
      el.save.disabled = !isOwner;
    });
  }

  el.save.addEventListener("click", save);

  function start() {
    var s = shell();
    var ready = s && s.ready ? s.ready : Promise.resolve({ role: null });
    return window.slAuth.requireLogin().then(function () {
      return ready;
    }).then(function (info) {
      isOwner = !!info && info.role === "owner";
      if (!orgId()) {
        showError("기관 정보를 찾지 못했어요. 다시 로그인해 주세요.");
        return null;
      }
      return loadSettings().then(function (ok) {
        return ok ? loadRetention() : null;
      });
    });
  }

  start().catch(function () {
    showError("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.");
  });

  document.addEventListener("DOMContentLoaded", function () {
    if (window.GYMotion) {
      window.GYMotion.enterPage();
    }
  });
})();
