// Shared app shell for staff screens: top bar (org name + switch sheet), bottom
// menu filtered by role, inbox badge, coach ring, toast, and the sheet helpers.
// Load order: vendor/gsap.min.js -> motion.js -> auth.js -> shell.js -> <screen>.js
// Screens only talk to window.slShell. Screens with data-chrome="none" do not load this file.
(function () {
  "use strict";

  var TOKEN_KEY = "sl_access_token";
  var ORG_KEY = "sl_org_id";
  var ROLE_KEY = "sl_role";
  var COACH_KEY = "sl_coach";
  var COACH_SEEN_KEY = "sl_coach_seen";
  var COACH_WAIT_MS = 2000;
  var ROLES = ["interviewer", "reviewer", "admin", "owner"];
  var ROLE_LABEL = {
    interviewer: "인터뷰어",
    reviewer: "검토자",
    admin: "관리자",
    owner: "소유자"
  };
  var NAV_SCREENS = ["home", "inbox", "cards", "dashboard"];
  var TOAST_MS = 2500;

  var ICONS = {
    home: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 10.5L12 4l8 6.5V20H4z" stroke="currentColor" stroke-width="1.6"/><path d="M9.5 20v-5h5v5" stroke="currentColor" stroke-width="1.6"/></svg>',
    inbox: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 13l2.5-8h11L20 13v6H4z" stroke="currentColor" stroke-width="1.6"/><path d="M4 13h5l1 2h4l1-2h5" stroke="currentColor" stroke-width="1.6"/></svg>',
    cards: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="5" y="4" width="14" height="16" rx="1.5" stroke="currentColor" stroke-width="1.6"/><path d="M8.5 9h7M8.5 12.5h7M8.5 16h4" stroke="currentColor" stroke-width="1.6"/></svg>',
    dashboard: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 20V10M12 20V4M19 20v-7" stroke="currentColor" stroke-width="1.8"/></svg>',
    chevron: '<svg viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M3 4.5l3 3 3-3" stroke="currentColor" stroke-width="1.4"/></svg>'
  };

  var STAFF_TOP =
    '<button type="button" class="org-btn" id="org-btn" aria-haspopup="dialog" aria-controls="sheet-org">' +
    '<span class="sq" id="org-initial" aria-hidden="true">·</span><span id="org-name-top">기관</span>' + ICONS.chevron +
    "</button>" +
    '<span class="role-label" id="role-label"></span>';

  var NAV =
    '<a data-nav="home" href="/" data-roles="interviewer reviewer admin owner">' + ICONS.home + "가게</a>" +
    '<a data-nav="inbox" href="/review" data-roles="reviewer admin owner">' +
    '<span class="badge" id="inbox-badge" hidden>0</span>' + ICONS.inbox + "검수함</a>" +
    '<a data-nav="cards" href="/cards" data-roles="interviewer reviewer admin owner">' + ICONS.cards + "공개 카드</a>" +
    '<a data-nav="dashboard" href="/dashboard" data-roles="admin owner">' + ICONS.dashboard + "기관</a>";

  var ORG_SHEET =
    '<div class="sh-h"><h2>기관 바꾸기</h2>' +
    '<button type="button" class="sheet-close" data-close aria-label="닫기">×</button></div>' +
    '<ul id="org-list"></ul>' +
    '<label class="switch"><input type="checkbox" id="coach-toggle"> 다음 할 일 표시</label>';

  var PUBLIC_TOP =
    '<span class="pub-title"><span class="mark" aria-hidden="true">遺</span>공개 카드</span>';

  // ---- storage helpers (storage can be unavailable: never throw) ----
  function lsGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function lsSet(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (e) {
      // storage unavailable
    }
  }

  function ssGet(key) {
    try {
      return window.sessionStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function ssSet(key, value) {
    try {
      if (value === null) {
        window.sessionStorage.removeItem(key);
      } else {
        window.sessionStorage.setItem(key, value);
      }
    } catch (e) {
      // storage unavailable
    }
  }

  function motion() {
    return window.GYMotion || null;
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function el(tag, attrs, html) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      node.setAttribute(k, attrs[k]);
    });
    if (html) {
      node.innerHTML = html;
    }
    return node;
  }

  // ---- token / authorised fetch without auth.js redirects ----
  function getToken() {
    if (window.slAuth && window.slAuth.getToken) {
      return window.slAuth.getToken().catch(function () {
        return null;
      });
    }
    return Promise.resolve(lsGet(TOKEN_KEY));
  }

  function authedJson(url, token) {
    var headers = { Authorization: "Bearer " + token };
    var org = lsGet(ORG_KEY);
    if (org) {
      headers["X-Org-Id"] = org;
    }
    return fetch(url, { headers: headers }).then(function (res) {
      return res.ok ? res.json() : null;
    }).catch(function () {
      return null;
    });
  }

  // ---- role ----
  var cachedRole = ROLES.indexOf(ssGet(ROLE_KEY)) >= 0 ? ssGet(ROLE_KEY) : null;

  function role() {
    return cachedRole;
  }

  function applyRole(r) {
    // Unknown role: show only what every role sees.
    var effective = r || "interviewer";
    var links = document.querySelectorAll("#app-nav a[data-roles]");
    Array.prototype.forEach.call(links, function (a) {
      a.hidden = a.getAttribute("data-roles").split(/\s+/).indexOf(effective) < 0;
    });
    var only = document.querySelectorAll("[data-only]");
    Array.prototype.forEach.call(only, function (node) {
      node.hidden = node.getAttribute("data-only").split(/\s+/).indexOf(effective) < 0;
    });
    var label = byId("role-label");
    if (label) {
      label.textContent = r ? ROLE_LABEL[r] : "";
      label.hidden = !r;
    }
  }

  // ---- current menu item ----
  function currentNav() {
    var main = document.querySelector("main[data-screen]");
    var screen = main ? main.getAttribute("data-screen") : "";
    if (NAV_SCREENS.indexOf(screen) >= 0) {
      return screen;
    }
    var path = window.location.pathname;
    if (/^\/shops\/[^/]+\/consent$/.test(path) || path.indexOf("/interview/") === 0 ||
        screen === "consent" || screen === "interview") {
      return "home";
    }
    if (path.indexOf("/review/") === 0 || screen === "review") {
      return "inbox";
    }
    if (path === "/members" || path === "/settings" || path.indexOf("/billing") === 0 ||
        screen === "members" || screen === "settings" || screen === "billing") {
      return "dashboard";
    }
    if (path.indexOf("/cards") === 0) {
      return "cards";
    }
    return "";
  }

  function markCurrent() {
    var cur = currentNav();
    var links = document.querySelectorAll("#app-nav a[data-nav]");
    Array.prototype.forEach.call(links, function (a) {
      if (a.getAttribute("data-nav") === cur) {
        a.setAttribute("aria-current", "page");
      } else {
        a.removeAttribute("aria-current");
      }
    });
  }

  // ---- sheets ----
  var openStack = [];

  function scrim() {
    return byId("scrim");
  }

  function hideCoach() {
    var ring = byId("coach");
    if (ring) {
      ring.style.opacity = "0";
    }
  }

  function openSheet(sheet) {
    if (!sheet) {
      return;
    }
    openStack.forEach(function (entry) {
      if (entry.sheet !== sheet) {
        entry.sheet.hidden = true;
      }
    });
    openStack = openStack.filter(function (entry) {
      return entry.sheet === sheet;
    });
    if (openStack.length === 0) {
      openStack.push({ sheet: sheet, returnTo: document.activeElement });
    }
    hideCoach();
    var m = motion();
    if (m) {
      m.openSheet(sheet, scrim());
    } else {
      sheet.hidden = false;
      if (scrim()) {
        scrim().hidden = false;
      }
      var focusable = sheet.querySelector("input,select,textarea,button");
      if (focusable) {
        focusable.focus({ preventScroll: true });
      }
    }
  }

  function closeSheet(sheet) {
    if (!sheet || sheet.hidden) {
      return;
    }
    var entry = null;
    openStack = openStack.filter(function (e) {
      if (e.sheet === sheet) {
        entry = e;
        return false;
      }
      return true;
    });
    var restore = function () {
      if (entry && entry.returnTo && entry.returnTo.focus && document.contains(entry.returnTo)) {
        entry.returnTo.focus({ preventScroll: true });
      }
    };
    var m = motion();
    var s = openStack.length === 0 ? scrim() : null;
    if (m) {
      m.closeSheet(sheet, s, restore);
    } else {
      sheet.hidden = true;
      if (s) {
        s.hidden = true;
      }
      restore();
    }
  }

  function topSheet() {
    return openStack.length ? openStack[openStack.length - 1].sheet : null;
  }

  function wireSheets() {
    document.addEventListener("click", function (ev) {
      var target = ev.target;
      if (!(target instanceof Element)) {
        return;
      }
      if (target.id === "scrim") {
        closeSheet(topSheet());
        return;
      }
      var closer = target.closest("[data-close]");
      if (closer) {
        var sheet = closer.closest(".sheet");
        if (sheet) {
          closeSheet(sheet);
        }
      }
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && topSheet()) {
        ev.preventDefault();
        closeSheet(topSheet());
      }
    });
  }

  // ---- badge / toast / coach / navigation ----
  function setBadge(n) {
    var badge = byId("inbox-badge");
    if (!badge) {
      return;
    }
    var count = Number(n) || 0;
    badge.textContent = String(count);
    badge.hidden = count <= 0;
  }

  var toastTimer = null;

  function toast(msg) {
    var t = byId("toast");
    if (!t) {
      return;
    }
    t.textContent = msg;
    t.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      t.classList.remove("show");
    }, TOAST_MS);
  }

  function coachOn() {
    return lsGet(COACH_KEY) !== "off";
  }

  function coach(targetEl) {
    var ring = byId("coach");
    var m = motion();
    if (!ring || !targetEl || !m || !coachOn() || topSheet()) {
      return;
    }
    var main = document.querySelector("main[data-screen]");
    var key = (main ? main.getAttribute("data-screen") : "") + " " + window.location.pathname;
    var seen = [];
    try {
      seen = JSON.parse(lsGet(COACH_SEEN_KEY) || "[]");
    } catch (e) {
      seen = [];
    }
    if (!Array.isArray(seen)) {
      seen = [];
    }
    if (seen.indexOf(key) >= 0) {
      return;
    }
    seen.push(key);
    lsSet(COACH_SEEN_KEY, JSON.stringify(seen));
    afterEnter([main, targetEl.closest("[data-stagger]")], function () {
      if (!topSheet()) {
        m.coach(ring, targetEl, document.body);
      }
    });
  }

  // Run fn once the enter tweens on these elements (page slide, stagger) have finished,
  // so the ring is measured where the target comes to rest.
  function afterEnter(els, fn) {
    var g = window.gsap;
    var targets = els.filter(Boolean);
    var started = Date.now();
    function pending() {
      if (!g || !g.getTweensOf || !targets.length) {
        return false;
      }
      return g.getTweensOf(targets).some(function (t) {
        return t.progress() < 1;
      });
    }
    function tick() {
      if (pending() && Date.now() - started < COACH_WAIT_MS) {
        window.requestAnimationFrame(tick);
        return;
      }
      fn();
    }
    tick();
  }

  function go(href, dir) {
    var m = motion();
    if (m) {
      m.leaveTo(href, dir || 1);
    } else {
      window.location.href = href;
    }
  }

  // ---- org switch sheet ----
  function renderOrgs(orgs, currentId) {
    var list = byId("org-list");
    if (!list) {
      return;
    }
    list.textContent = "";
    orgs.forEach(function (o) {
      var li = document.createElement("li");
      var b = el("button", { type: "button", "class": "btn btn-sec" });
      var name = document.createElement("span");
      name.textContent = o.name;
      var meta = el("span", { "class": "hint" });
      var isCurrent = o.org_id === currentId;
      meta.textContent = (ROLE_LABEL[o.role] || "") + (isCurrent ? " · 지금" : "");
      if (isCurrent) {
        b.setAttribute("aria-current", "true");
      }
      b.appendChild(name);
      b.appendChild(meta);
      b.addEventListener("click", function () {
        if (isCurrent) {
          closeSheet(byId("sheet-org"));
          return;
        }
        lsSet(ORG_KEY, o.org_id);
        ssSet(ROLE_KEY, null);
        window.location.reload();
      });
      li.appendChild(b);
      list.appendChild(li);
    });
  }

  function showOrg(name) {
    var top = byId("org-name-top");
    var initial = byId("org-initial");
    if (top) {
      top.textContent = name || "기관";
    }
    if (initial) {
      initial.textContent = name ? name.charAt(0) : "·";
    }
  }

  // ---- DOM build ----
  function buildCommon(body) {
    body.appendChild(el("div", { "class": "scrim", id: "scrim", hidden: "" }));
    body.appendChild(el("div", { "class": "toast", id: "toast", role: "status", "aria-live": "polite" }));
    body.appendChild(el("div", { "class": "coach", id: "coach", "aria-hidden": "true" }));
  }

  function buildStaff(body) {
    body.classList.add("signed-in");
    var top = el("header", { "class": "app-top", id: "chrome-staff" }, STAFF_TOP);
    body.insertBefore(top, body.firstChild);
    var nav = el("nav", { "class": "app-nav", id: "app-nav", "aria-label": "앱 메뉴" }, NAV);
    body.insertBefore(nav, byId("scrim"));
    var sheet = el("div", {
      "class": "sheet",
      id: "sheet-org",
      hidden: "",
      role: "dialog",
      "aria-modal": "true",
      "aria-label": "기관 바꾸기"
    }, ORG_SHEET);
    if (!byId("auth-link")) {
      sheet.appendChild(el("a", { id: "auth-link", href: "/login" }, "로그아웃"));
    }
    body.appendChild(sheet);

    var toggle = byId("coach-toggle");
    toggle.checked = coachOn();
    toggle.addEventListener("change", function () {
      lsSet(COACH_KEY, toggle.checked ? "on" : "off");
      if (!toggle.checked) {
        hideCoach();
      }
    });
    byId("org-btn").addEventListener("click", function () {
      openSheet(sheet);
    });
    markCurrent();
    applyRole(cachedRole);
  }

  function buildPublic(body) {
    var top = el("header", { "class": "app-top", id: "chrome-public" }, PUBLIC_TOP);
    if (!byId("auth-link")) {
      top.appendChild(el("a", { id: "auth-link", href: "/login" }, "로그인"));
    }
    body.insertBefore(top, body.firstChild);
  }

  function bindAuthLink() {
    if (window.slAuth && window.slAuth.bindAuthLink) {
      window.slAuth.bindAuthLink(byId("auth-link"));
    }
  }

  function loadMe(token) {
    return authedJson("/api/me", token).then(function (me) {
      var orgs = me && Array.isArray(me.orgs) ? me.orgs : [];
      var orgId = lsGet(ORG_KEY);
      var mine = null;
      orgs.forEach(function (o) {
        if (o.org_id === orgId) {
          mine = o;
        }
      });
      if (!mine && orgs.length > 0) {
        mine = orgs[0];
        lsSet(ORG_KEY, mine.org_id);
      }
      if (me && Array.isArray(me.orgs) && orgs.length === 0 && orgId) {
        try {
          window.localStorage.removeItem(ORG_KEY);
        } catch (e) {
          // storage unavailable
        }
      }
      var r = mine && ROLES.indexOf(mine.role) >= 0 ? mine.role : null;
      if (me) {
        cachedRole = r;
        ssSet(ROLE_KEY, r);
      }
      renderOrgs(orgs, mine ? mine.org_id : null);
      showOrg(mine ? mine.name : null);
      applyRole(cachedRole);
      if (cachedRole && cachedRole !== "interviewer") {
        refreshBadge(token);
      }
      return { role: cachedRole, orgId: mine ? mine.org_id : null, orgName: mine ? mine.name : null };
    });
  }

  function refreshBadge(token) {
    authedJson("/api/review/inbox", token).then(function (data) {
      var items = data && Array.isArray(data.items) ? data.items : [];
      var n = 0;
      items.forEach(function (it) {
        var decided = it.owner_decision !== null && it.owner_decision !== undefined;
        if (it.status === "draft" && !decided) {
          n += 1;
        } else if (decided && it.status !== "published") {
          n += 1;
        }
      });
      setBadge(n);
    });
  }

  var resolveReady;
  var ready = new Promise(function (resolve) {
    resolveReady = resolve;
  });
  var EMPTY = { role: null, orgId: null, orgName: null };

  function init() {
    var body = document.body;
    var chrome = body.getAttribute("data-chrome");
    buildCommon(body);
    wireSheets();
    if (chrome === "staff") {
      buildStaff(body);
      bindAuthLink();
      getToken().then(function (token) {
        return token ? loadMe(token) : EMPTY;
      }).then(resolveReady, function () {
        resolveReady(EMPTY);
      });
      return;
    }
    // public: signed-in staff get the same chrome, visitors get a title + login link.
    getToken().then(function (token) {
      if (token) {
        buildStaff(body);
        bindAuthLink();
        return loadMe(token);
      }
      buildPublic(body);
      bindAuthLink();
      return EMPTY;
    }).then(resolveReady, function () {
      resolveReady(EMPTY);
    });
  }

  window.slShell = {
    ready: ready,
    role: role,
    openSheet: openSheet,
    closeSheet: closeSheet,
    setBadge: setBadge,
    coach: coach,
    toast: toast,
    go: go
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
