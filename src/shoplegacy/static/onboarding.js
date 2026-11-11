(function () {
  "use strict";

  var ORG_KEY = "sl_org_id";
  var MSG_EMPTY = "기관 이름을 입력해 주세요.";
  var MSG_LIMIT = "요금제 한도로 기관을 더 만들 수 없습니다.";
  var MSG_FAIL = "기관을 만들지 못했습니다. 잠시 후 다시 시도해 주세요.";

  var form = document.getElementById("onboarding-form");
  var nameEl = document.getElementById("org-name");
  var submitBtn = document.getElementById("create-org");
  var errorEl = document.getElementById("onboarding-error");

  function nextPath() {
    var next = new URLSearchParams(window.location.search).get("next") || "/";
    // Only same-site paths: never redirect to another origin.
    if (next.charAt(0) !== "/" || next.charAt(1) === "/" || next.charAt(1) === "\\") {
      return "/";
    }
    return next;
  }

  function showError(text) {
    errorEl.textContent = text;
    errorEl.hidden = false;
  }

  function clearError() {
    errorEl.textContent = "";
    errorEl.hidden = true;
  }

  window.slAuth.requireLogin();

  if (window.GYMotion) {
    window.GYMotion.enterPage();
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    clearError();
    var name = nameEl.value.trim();
    if (name === "") {
      showError(MSG_EMPTY);
      return;
    }
    submitBtn.disabled = true;
    window.slAuth.apiFetch("/api/orgs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name })
    })
      .then(function (res) {
        if (res.status === 201) {
          return res.json().then(function (body) {
            window.localStorage.setItem(ORG_KEY, body.org_id);
            window.location.href = nextPath();
          });
        }
        showError(res.status === 402 ? MSG_LIMIT : MSG_FAIL);
        submitBtn.disabled = false;
      })
      .catch(function () {
        showError(MSG_FAIL);
        submitBtn.disabled = false;
      });
  });
})();
