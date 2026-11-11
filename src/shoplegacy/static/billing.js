(function () {
  "use strict";

  var ORG_KEY = "sl_org_id";
  var PLAN_LABELS = { free: "무료 체험", pilot: "시범 사업", org: "기관" };
  var TOSS_SDK_URL = "https://js.tosspayments.com/v2/standard";
  var AUTOPAY_PLAN_KEY = "sl_autopay_plan";

  var el = {
    error: document.getElementById("billing-error"),
    name: document.getElementById("plan-name"),
    period: document.getElementById("plan-period"),
    shops: document.getElementById("usage-shops"),
    llm: document.getElementById("usage-llm"),
    members: document.getElementById("usage-members"),
    shopsBar: document.getElementById("usage-shops-bar"),
    llmBar: document.getElementById("usage-llm-bar"),
    membersBar: document.getElementById("usage-members-bar"),
    limit: document.getElementById("plan-limit"),
    invoiceScroll: document.getElementById("invoice-scroll"),
    billingMsg: document.getElementById("billing-message"),
    invoiceTable: document.getElementById("invoice-table"),
    invoiceBody: document.querySelector("#invoice-table tbody"),
    invoiceEmpty: document.getElementById("invoice-empty"),
    widgetPanel: document.getElementById("widget-panel"),
    widgetPay: document.getElementById("widget-pay"),
    autopayStatus: document.getElementById("autopay-status"),
    autopayCard: document.getElementById("autopay-card"),
    autopayNext: document.getElementById("autopay-next"),
    autopayPlan: document.getElementById("autopay-plan"),
    autopayRegister: document.getElementById("autopay-register"),
    autopayStop: document.getElementById("autopay-stop"),
    autopayMsg: document.getElementById("autopay-message")
  };

  var STATUS_LABELS = { issued: "미결제", paid: "결제 완료", void: "취소", refunded: "환불됨" };
  var AUTOPAY_LABELS = { active: "사용 중", stopped: "중지됨" };

  var isOwner = false;
  var currentPlan = null;
  var widgetSession = null;
  var sdkPromise = null;

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

  function showError(msg) {
    el.error.textContent = msg;
    el.error.hidden = false;
  }

  function clearError() {
    el.error.textContent = "";
    el.error.hidden = true;
  }

  function motion() {
    return window.GYMotion || null;
  }

  function fmt(n) {
    return Number(n).toLocaleString("ko-KR");
  }

  function usageText(used, limit) {
    return fmt(used) + " / " + fmt(limit);
  }

  function periodText(period) {
    var m = /^(\d{4})-(\d{2})$/.exec(period || "");
    return m ? m[1] + "년 " + Number(m[2]) + "월" : (period || "-");
  }

  function readJson(res) {
    return res.json().catch(function () {
      return {};
    });
  }

  var WARN_RATIO = 0.8;

  // Fill one usage meter and return how close it is to the limit.
  function renderMeter(bar, used, limit) {
    var ratio = limit > 0 ? Math.min(used / limit, 1) : 0;
    var meter = bar.parentNode.parentNode;
    meter.classList.toggle("warn", ratio >= WARN_RATIO);
    var m = motion();
    if (m) {
      m.fillBar(bar, ratio);
    } else {
      bar.style.transform = "scaleX(" + ratio + ")";
    }
    return ratio;
  }

  function limitSentence(items) {
    var full = items.filter(function (it) {
      return it.ratio >= 1;
    });
    var near = items.filter(function (it) {
      return it.ratio >= WARN_RATIO && it.ratio < 1;
    });
    if (full.length) {
      return {
        warn: true,
        text: full.map(function (it) { return it.label; }).join("·") +
          " 한도에 닿았어요. 더 쓰려면 요금제를 바꿔야 해요."
      };
    }
    if (near.length) {
      return {
        warn: true,
        text: near.map(function (it) { return it.label; }).join("·") +
          " 사용량이 한도의 80%를 넘었어요."
      };
    }
    return { warn: false, text: "이번 달 사용량은 한도 안에 있어요." };
  }

  function renderPlan(info) {
    currentPlan = info.plan;
    el.name.textContent = PLAN_LABELS[info.plan] || info.plan;
    el.period.textContent = periodText(info.period);
    el.shops.textContent = usageText(info.usage.shops, info.limits.shops);
    el.llm.textContent = usageText(info.usage.llm_calls_month, info.limits.llm_calls_month);
    el.members.textContent = usageText(info.usage.members, info.limits.members);
    var sentence = limitSentence([
      { label: "참여 가게", ratio: renderMeter(el.shopsBar, info.usage.shops, info.limits.shops) },
      { label: "AI 사용", ratio: renderMeter(el.llmBar, info.usage.llm_calls_month, info.limits.llm_calls_month) },
      { label: "구성원", ratio: renderMeter(el.membersBar, info.usage.members, info.limits.members) }
    ]);
    el.limit.textContent = sentence.text;
    el.limit.classList.toggle("warn", sentence.warn);
    el.limit.hidden = false;
  }

  function loadPlan() {
    return window.slAuth.apiFetch(apiBase() + "/plan").then(function (res) {
      return readJson(res).then(function (body) {
        if (!res.ok) {
          showError(res.status === 403
            ? "이 기관의 요금제를 볼 권한이 없어요."
            : "요금제 정보를 불러오지 못했어요. 잠시 후 다시 시도해 주세요. (" + res.status + ")");
          return;
        }
        renderPlan(body);
      });
    });
  }

  function loadRole() {
    return window.slAuth.apiFetch("/api/me").then(function (res) {
      return res.ok ? readJson(res) : {};
    }).then(function (me) {
      var org = (orgId() || "").toLowerCase();
      var mine = (me.orgs || []).filter(function (o) {
        return String(o.org_id).toLowerCase() === org;
      })[0];
      return mine ? mine.role : null;
    });
  }

  function showBillingMsg(msg, isError) {
    el.billingMsg.textContent = msg;
    el.billingMsg.className = isError ? "error" : "info";
    el.billingMsg.hidden = false;
  }

  function invoicePeriodText(start, end) {
    var s = /^(\d{4})-(\d{2})-(\d{2})$/.exec(start || "");
    var e = /^(\d{4})-(\d{2})-(\d{2})$/.exec(end || "");
    if (s && e && s[3] === "01" && e[3] === "01") {
      var next = new Date(Date.UTC(Number(s[1]), Number(s[2]), 1));
      if (next.getUTCFullYear() === Number(e[1]) && next.getUTCMonth() + 1 === Number(e[2])) {
        return s[1] + "년 " + Number(s[2]) + "월";
      }
    }
    return (start || "-") + " ~ " + (end || "-");
  }

  function cell(tr, cls, text) {
    var td = document.createElement("td");
    td.className = cls;
    td.textContent = text;
    tr.appendChild(td);
    return td;
  }

  function renderInvoices(list) {
    el.invoiceBody.textContent = "";
    if (!list.length) {
      el.invoiceTable.hidden = true;
      el.invoiceScroll.hidden = true;
      el.invoiceEmpty.hidden = false;
      return;
    }
    el.invoiceEmpty.hidden = true;
    el.invoiceScroll.hidden = false;
    el.invoiceTable.hidden = false;
    list.forEach(function (inv) {
      var tr = document.createElement("tr");
      tr.setAttribute("data-invoice-id", inv.invoice_id);
      var planLabel = PLAN_LABELS[inv.plan] || inv.plan;
      var periodLabel = invoicePeriodText(inv.period_start, inv.period_end);
      cell(tr, "invoice-plan", planLabel);
      cell(tr, "invoice-period", periodLabel);
      cell(tr, "invoice-amount num", fmt(inv.amount_krw) + "원");
      cell(tr, "invoice-status", STATUS_LABELS[inv.status] || inv.status);
      cell(tr, "invoice-tax", inv.tax_invoice_ref || "-");
      var action = cell(tr, "invoice-action", "");
      if (inv.status === "issued" && isOwner) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "invoice-pay btn";
        btn.textContent = "결제";
        btn.setAttribute("aria-label", periodLabel + " " + planLabel + " 청구서 결제");
        btn.addEventListener("click", function () {
          pay(inv, btn);
        });
        action.appendChild(btn);
      }
      el.invoiceBody.appendChild(tr);
    });
  }

  function loadInvoices() {
    return window.slAuth.apiFetch(apiBase() + "/invoices").then(function (res) {
      return readJson(res).then(function (body) {
        if (!res.ok) {
          showBillingMsg(res.status === 403
            ? "이 기관의 청구서를 볼 권한이 없어요."
            : "청구서를 불러오지 못했어요. 잠시 후 다시 시도해 주세요. (" + res.status + ")", true);
          return;
        }
        renderInvoices(Array.isArray(body) ? body : []);
      });
    });
  }

  function pay(inv, btn) {
    btn.disabled = true;
    window.slAuth.apiFetch(apiBase() + "/invoices/" + encodeURIComponent(inv.invoice_id) + "/checkout", {
      method: "POST"
    }).then(function (res) {
      return readJson(res).then(function (body) {
        if (!res.ok) {
          showBillingMsg(res.status === 403
            ? "결제는 기관 소유자만 할 수 있어요."
            : res.status === 409
              ? "이미 결제됐거나 결제할 수 없는 청구서예요."
              : "결제를 시작하지 못했어요. 잠시 후 다시 시도해 주세요. (" + res.status + ")", true);
          btn.disabled = false;
          return;
        }
        if (body.checkout_url) {
          window.location.href = body.checkout_url;
          return;
        }
        if (body.client_key) {
          return openWidget(body, inv).then(function () {
            btn.disabled = false;
          });
        }
        showBillingMsg("결제를 시작하지 못했어요. 담당자에게 문의해 주세요.", true);
        btn.disabled = false;
      });
    }).catch(function () {
      showBillingMsg("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.", true);
      btn.disabled = false;
    });
  }

  function loadTossSdk() {
    if (typeof window.TossPayments === "function") {
      return Promise.resolve(window.TossPayments);
    }
    if (!sdkPromise) {
      sdkPromise = new Promise(function (resolve, reject) {
        var script = document.createElement("script");
        script.src = TOSS_SDK_URL;
        script.onload = function () {
          if (typeof window.TossPayments === "function") {
            resolve(window.TossPayments);
          } else {
            reject(new Error("sdk"));
          }
        };
        script.onerror = function () {
          reject(new Error("sdk"));
        };
        document.head.appendChild(script);
      });
      sdkPromise.catch(function () {
        sdkPromise = null;
      });
    }
    return sdkPromise;
  }

  function customerKeyOf() {
    return "sl-" + (orgId() || "").toLowerCase();
  }

  function openWidget(session, inv) {
    return loadTossSdk().then(function (TossPayments) {
      var widgets = TossPayments(session.client_key).widgets({ customerKey: customerKeyOf() });
      el.widgetPanel.hidden = false;
      return Promise.resolve(widgets.setAmount({ currency: "KRW", value: session.amount_krw })).then(function () {
        return Promise.all([
          widgets.renderPaymentMethods({ selector: "#payment-method", variantKey: "DEFAULT" }),
          widgets.renderAgreement({ selector: "#agreement", variantKey: "AGREEMENT" })
        ]);
      }).then(function () {
        widgetSession = { widgets: widgets, session: session, plan: inv.plan };
        el.widgetPay.disabled = false;
        el.widgetPay.focus();
      });
    }).catch(function () {
      showBillingMsg("결제 화면을 불러오지 못했어요. 잠시 후 다시 시도해 주세요.", true);
    });
  }

  function widgetPay() {
    if (!widgetSession) {
      return;
    }
    var s = widgetSession.session;
    el.widgetPay.disabled = true;
    Promise.resolve(widgetSession.widgets.requestPayment({
      orderId: s.order_id,
      orderName: "가게유산 " + widgetSession.plan,
      successUrl: s.success_url,
      failUrl: s.fail_url
    })).catch(function () {
      showBillingMsg("결제가 취소됐거나 실패했어요. 다시 시도해 주세요.", true);
      el.widgetPay.disabled = false;
    });
  }

  function confirmReturn(params) {
    var amount = parseInt(params.get("amount") || "", 10);
    return window.slAuth.apiFetch("/api/billing/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        payment_key: params.get("paymentKey"),
        order_id: params.get("orderId"),
        amount_krw: amount
      })
    }).then(function (res) {
      if (res.ok) {
        showBillingMsg("결제가 완료됐어요", false);
        return;
      }
      showBillingMsg(res.status === 402
        ? "카드 결제가 승인되지 않았어요. 다시 시도해 주세요."
        : res.status === 409
          ? "결제 금액이 맞지 않거나 이미 처리된 청구서예요."
          : res.status === 403
            ? "결제는 기관 소유자만 할 수 있어요."
            : "결제를 확인하지 못했어요. 잠시 후 다시 시도해 주세요. (" + res.status + ")", true);
    }).catch(function () {
      showBillingMsg("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.", true);
    });
  }

  // Returns a promise that settles once any payment return in the URL is handled.
  function handlePaymentReturn() {
    var params = new URLSearchParams(window.location.search);
    if (params.get("paymentKey") && params.get("orderId") && params.get("amount")) {
      return confirmReturn(params);
    }
    if (params.get("paid")) {
      showBillingMsg("결제가 완료됐어요", false);
    } else if (params.get("failed")) {
      showBillingMsg("결제가 취소됐거나 실패했어요. 다시 시도해 주세요.", true);
    }
    return Promise.resolve();
  }

  function showAutopayMsg(msg, isError) {
    el.autopayMsg.textContent = msg;
    el.autopayMsg.className = isError ? "error" : "info";
    el.autopayMsg.hidden = false;
  }

  function renderAutopay(key) {
    if (!key) {
      el.autopayStatus.textContent = "등록된 카드가 없어요";
      el.autopayCard.textContent = "-";
      el.autopayNext.textContent = "-";
      el.autopayPlan.value = currentPlan === "org" ? "org" : "pilot";
    } else {
      el.autopayStatus.textContent = AUTOPAY_LABELS[key.status] || key.status;
      el.autopayCard.textContent = key.card_label || "-";
      el.autopayNext.textContent = key.status === "active" ? (key.next_charge_on || "-") : "-";
      if (key.plan === "pilot" || key.plan === "org") {
        el.autopayPlan.value = key.plan;
      }
    }
    var active = !!key && key.status === "active";
    el.autopayPlan.disabled = !isOwner;
    el.autopayRegister.disabled = !isOwner;
    el.autopayRegister.textContent = active ? "카드 다시 등록" : "카드 등록";
    el.autopayStop.hidden = !active;
    el.autopayStop.disabled = !(active && isOwner);
  }

  function loadAutopay() {
    return window.slAuth.apiFetch(apiBase() + "/billing-key").then(function (res) {
      return readJson(res).then(function (body) {
        if (res.status === 404) {
          renderAutopay(null);
          return;
        }
        if (!res.ok) {
          renderAutopay(null);
          el.autopayStatus.textContent = "-";
          showAutopayMsg(res.status === 403
            ? "이 기관의 자동결제 정보를 볼 권한이 없어요."
            : "자동결제 정보를 불러오지 못했어요. (" + res.status + ")", true);
          return;
        }
        renderAutopay(body);
      });
    });
  }

  function defaultAutopayPlan() {
    var saved = null;
    try {
      saved = window.sessionStorage.getItem(AUTOPAY_PLAN_KEY);
      window.sessionStorage.removeItem(AUTOPAY_PLAN_KEY);
    } catch (e) {
      saved = null;
    }
    if (saved === "pilot" || saved === "org") {
      return saved;
    }
    return currentPlan === "org" ? "org" : "pilot";
  }

  function registerCard() {
    el.autopayMsg.hidden = true;
    el.autopayRegister.disabled = true;
    var plan = el.autopayPlan.value === "org" ? "org" : "pilot";
    try {
      window.sessionStorage.setItem(AUTOPAY_PLAN_KEY, plan);
    } catch (e) {
      // Without storage the plan falls back to the current one on return.
    }
    window.slAuth.apiFetch(apiBase() + "/billing-key/auth", { method: "POST" }).then(function (res) {
      return readJson(res).then(function (body) {
        if (!res.ok) {
          showAutopayMsg(res.status === 403
            ? "카드 등록은 기관 소유자만 할 수 있어요."
            : "카드 등록을 시작하지 못했어요. 잠시 후 다시 시도해 주세요. (" + res.status + ")", true);
          el.autopayRegister.disabled = !isOwner;
          return;
        }
        if (body.auth_url) {
          window.location.href = body.auth_url;
          return;
        }
        if (!body.client_key) {
          showAutopayMsg("카드 등록을 시작하지 못했어요. 담당자에게 문의해 주세요.", true);
          el.autopayRegister.disabled = !isOwner;
          return;
        }
        return loadTossSdk().then(function (TossPayments) {
          return TossPayments(body.client_key).payment({ customerKey: body.customer_key }).requestBillingAuth({
            method: "CARD",
            successUrl: body.success_url,
            failUrl: body.fail_url
          });
        }).catch(function () {
          showAutopayMsg("카드 등록이 취소됐거나 실패했어요. 다시 시도해 주세요.", true);
          el.autopayRegister.disabled = !isOwner;
        });
      });
    }).catch(function () {
      showAutopayMsg("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.", true);
      el.autopayRegister.disabled = !isOwner;
    });
  }

  // Returns a promise that settles once a card-registration return in the URL is handled.
  function handleAutopayReturn() {
    var params = new URLSearchParams(window.location.search);
    if (params.get("billing_failed")) {
      showAutopayMsg("카드 등록이 취소됐거나 실패했어요. 다시 시도해 주세요.", true);
      return Promise.resolve();
    }
    if (!params.get("billing_auth") || !params.get("authKey") || !params.get("customerKey")) {
      return Promise.resolve();
    }
    var plan = defaultAutopayPlan();
    return window.slAuth.apiFetch(apiBase() + "/billing-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        auth_key: params.get("authKey"),
        customer_key: params.get("customerKey"),
        plan: plan,
        charge_day: 1
      })
    }).then(function (res) {
      return readJson(res).then(function (body) {
        if (!res.ok) {
          showAutopayMsg(res.status === 402
            ? "카드를 등록하지 못했어요. 카드 정보를 확인하고 다시 시도해 주세요."
            : res.status === 403
              ? "카드 등록은 기관 소유자만 할 수 있어요."
              : "카드를 등록하지 못했어요. 잠시 후 다시 시도해 주세요. (" + res.status + ")", true);
          return;
        }
        showAutopayMsg("카드를 등록했어요. 다음 결제일: " + (body.next_charge_on || "-"), false);
      });
    }).catch(function () {
      showAutopayMsg("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.", true);
    });
  }

  function stopAutopay() {
    el.autopayMsg.hidden = true;
    el.autopayStop.disabled = true;
    window.slAuth.apiFetch(apiBase() + "/billing-key", { method: "DELETE" }).then(function (res) {
      if (!res.ok && res.status !== 404) {
        showAutopayMsg(res.status === 403
          ? "자동결제는 기관 소유자만 중지할 수 있어요."
          : "자동결제를 중지하지 못했어요. 잠시 후 다시 시도해 주세요. (" + res.status + ")", true);
        el.autopayStop.disabled = !isOwner;
        return;
      }
      el.autopayStatus.textContent = AUTOPAY_LABELS.stopped;
      el.autopayNext.textContent = "-";
      el.autopayStop.hidden = true;
      showAutopayMsg("자동결제를 중지했어요", false);
      return loadAutopay();
    }).catch(function () {
      showAutopayMsg("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.", true);
      el.autopayStop.disabled = !isOwner;
    });
  }

  el.widgetPay.addEventListener("click", widgetPay);
  el.autopayRegister.addEventListener("click", registerCard);
  el.autopayStop.addEventListener("click", stopAutopay);

  // Point the coach ring at the first unpaid invoice, or card registration when nothing is due.
  function coachNext() {
    var s = window.slShell;
    if (!s || !s.coach || !isOwner) {
      return;
    }
    var target = el.invoiceBody.querySelector("button.invoice-pay") ||
      (el.autopayRegister.disabled ? null : el.autopayRegister);
    if (!target) {
      return;
    }
    // Wait for the page enter slide so the ring is measured in place.
    window.setTimeout(function () {
      s.coach(target);
    }, 500);
  }

  window.slAuth.requireLogin().then(function () {
    return window.slAuth.getToken();
  }).then(function (token) {
    if (!token) {
      return null;
    }
    clearError();
    // /api/me also stores sl_org_id on a fresh login, so it runs first.
    return loadRole().then(function (role) {
      if (!orgId()) {
        showError("기관 정보를 찾지 못했어요. 다시 로그인해 주세요.");
        return null;
      }
      isOwner = role === "owner";
      return Promise.all([
        loadPlan().then(handleAutopayReturn).then(loadAutopay),
        handlePaymentReturn().then(loadInvoices)
      ]).then(coachNext);
    });
  }).catch(function () {
    showError("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.");
  });

  document.addEventListener("DOMContentLoaded", function () {
    if (motion()) {
      motion().enterPage();
    }
  });
})();
