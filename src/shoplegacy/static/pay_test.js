(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    if (window.GYMotion) {
      window.GYMotion.enterPage();
    }
  });

  // Point the coach ring at the confirm button once the enter slide has settled.
  function coachConfirm() {
    var s = window.slShell;
    var btn = document.getElementById("pay-confirm");
    if (!s || !s.coach || !btn || btn.disabled) {
      return;
    }
    window.setTimeout(function () {
      s.coach(btn);
    }, 500);
  }

  var params = new URLSearchParams(window.location.search);
  var orderId = params.get("orderId") || "";
  var amount = parseInt(params.get("amount") || "", 10);
  var successUrl = params.get("successUrl") || "";
  var billingMode = params.get("mode") === "billing";
  var customerKey = params.get("customerKey") || "";

  var el = {
    title: document.querySelector("#pay-test h1"),
    order: document.getElementById("pay-order"),
    amount: document.getElementById("pay-amount"),
    amountRow: document.getElementById("pay-amount-row"),
    confirm: document.getElementById("pay-confirm"),
    result: document.getElementById("pay-result")
  };

  function showResult(msg, isError) {
    el.result.textContent = msg;
    el.result.className = isError ? "error" : "info";
  }

  // Only follow a return address on this same origin.
  function safeReturnUrl(raw) {
    if (!raw) {
      return null;
    }
    try {
      var url = new URL(raw, window.location.href);
      return url.origin === window.location.origin ? url.href : null;
    } catch (e) {
      return null;
    }
  }

  // Card registration for automatic payment: no API call, just return with a test auth key.
  if (billingMode) {
    document.title = "가게유산 · 카드 등록 (테스트)";
    el.title.textContent = "카드 등록 (테스트)";
    el.amountRow.hidden = true;
    el.order.previousElementSibling.textContent = "고객 번호";
    el.order.textContent = customerKey || "-";
    el.confirm.textContent = "카드 등록";
    var back = safeReturnUrl(successUrl);
    if (!customerKey || !back) {
      el.confirm.disabled = true;
      showResult("카드 등록 정보가 없어요. 요금제 화면에서 다시 시작해 주세요.", true);
      return;
    }
    el.confirm.addEventListener("click", function () {
      el.confirm.disabled = true;
      showResult("카드를 등록하는 중…", false);
      window.location.href = back + (back.indexOf("?") >= 0 ? "&" : "?") +
        "customerKey=" + encodeURIComponent(customerKey) +
        "&authKey=" + encodeURIComponent("fake_auth_" + customerKey);
    });
    window.slAuth.requireLogin().then(coachConfirm);
    return;
  }

  el.order.textContent = orderId || "-";
  el.amount.textContent = isNaN(amount) ? "-" : amount.toLocaleString("ko-KR") + "원";

  if (!orderId || isNaN(amount)) {
    el.confirm.disabled = true;
    showResult("결제 정보가 없어요. 요금제 화면에서 다시 시작해 주세요.", true);
    return;
  }

  el.confirm.addEventListener("click", function () {
    el.confirm.disabled = true;
    showResult("결제하는 중…", false);
    window.slAuth.apiFetch("/api/billing/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ payment_key: "fake_" + orderId, order_id: orderId, amount_krw: amount })
    }).then(function (res) {
      return res.json().catch(function () {
        return {};
      }).then(function (body) {
        if (!res.ok) {
          var detail = typeof body.detail === "string" ? body.detail : "status " + res.status;
          showResult("결제하지 못했어요: " + detail, true);
          el.confirm.disabled = false;
          return;
        }
        showResult("결제 완료", false);
        var next = safeReturnUrl(successUrl);
        if (next) {
          window.setTimeout(function () {
            window.location.href = next;
          }, 1000);
        }
      });
    }).catch(function () {
      showResult("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.", true);
      el.confirm.disabled = false;
    });
  });

  window.slAuth.requireLogin().then(coachConfirm);
})();
