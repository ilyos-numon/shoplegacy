(function () {
  "use strict";

  // Pinned on purpose (released 2026-10-07). Bump deliberately, never float.
  var SUPABASE_JS_URL = "https://esm.sh/@supabase/supabase-js@2.117.3";
  var TOKEN_KEY = "sl_access_token";
  var ORG_KEY = "sl_org_id";
  var ONBOARDING_PATH = "/onboarding";

  var clientPromise = null;

  function stored(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function currentPath() {
    return window.location.pathname + window.location.search;
  }

  function loginUrl() {
    return "/login?next=" + encodeURIComponent(currentPath());
  }

  function onboardingUrl() {
    return ONBOARDING_PATH + "?next=" + encodeURIComponent(currentPath());
  }

  function canRedirectToOnboarding() {
    var path = window.location.pathname;
    return path !== "/onboarding" && path !== "/login";
  }

  // Resolves to a supabase client, or null when the CDN or config is unavailable.
  // Rejects never: callers fall back to the localStorage token path.
  function getClient() {
    if (clientPromise) {
      return clientPromise;
    }
    clientPromise = fetch("/api/auth/config")
      .then(function (res) {
        if (!res.ok) {
          throw new Error("auth config " + res.status);
        }
        return res.json();
      })
      .then(function (cfg) {
        if (!cfg.supabase_url || !cfg.anon_key) {
          return null;
        }
        return import(SUPABASE_JS_URL).then(function (mod) {
          return mod.createClient(cfg.supabase_url, cfg.anon_key);
        });
      })
      .catch(function () {
        return null;
      });
    return clientPromise;
  }

  function getToken() {
    var token = stored(TOKEN_KEY);
    if (token) {
      return Promise.resolve(token);
    }
    return getClient().then(function (client) {
      if (!client) {
        return null;
      }
      return client.auth.getSession().then(function (result) {
        var session = result && result.data ? result.data.session : null;
        return session ? session.access_token : null;
      });
    }).catch(function () {
      return null;
    });
  }

  function clearOrg() {
    try {
      window.localStorage.removeItem(ORG_KEY);
    } catch (e) {
      // storage unavailable: nothing to clear
    }
  }

  // Checked once per page: the stored org must be one of my memberships. A login
  // with no org (or a stale one, e.g. after a DB reset) takes the first membership;
  // no membership at all clears it and sends the user to onboarding.
  // Network failure leaves the stored value alone.
  var orgCheck = null;

  function ensureOrg(token) {
    if (!token) {
      return Promise.resolve();
    }
    if (orgCheck) {
      return orgCheck;
    }
    orgCheck = fetch("/api/me", { headers: { Authorization: "Bearer " + token } })
      .then(function (res) {
        return res.ok ? res.json() : null;
      })
      .then(function (me) {
        if (!me || !Array.isArray(me.orgs)) {
          return;
        }
        var current = stored(ORG_KEY);
        var mine = me.orgs.some(function (o) {
          return o.org_id === current;
        });
        if (mine) {
          return;
        }
        if (me.orgs.length > 0) {
          window.localStorage.setItem(ORG_KEY, me.orgs[0].org_id);
          return;
        }
        clearOrg();
        if (canRedirectToOnboarding()) {
          window.location.href = onboardingUrl();
        }
      })
      .catch(function () {});
    return orgCheck;
  }

  function apiFetch(url, init) {
    var opts = Object.assign({}, init || {});
    return getToken().then(function (token) {
      return ensureOrg(token).then(function () {
        var headers = new Headers(opts.headers || {});
        if (token) {
          headers.set("Authorization", "Bearer " + token);
        }
        var org = stored(ORG_KEY);
        if (org) {
          headers.set("X-Org-Id", org);
        }
        opts.headers = headers;
        return fetch(url, opts);
      });
    }).then(function (res) {
      if (res.status === 401 && window.location.pathname !== "/login") {
        window.location.href = loginUrl();
        return res;
      }
      if (res.status === 400 && canRedirectToOnboarding()) {
        return res.clone().json().then(function (body) {
          if (body && body.detail === "org_required" && canRedirectToOnboarding()) {
            window.location.href = onboardingUrl();
          }
          return res;
        }, function () {
          return res;
        });
      }
      return res;
    });
  }

  function requireLogin() {
    return getToken().then(function (token) {
      if (!token) {
        window.location.href = loginUrl();
      }
    });
  }

  function logout() {
    try {
      window.localStorage.removeItem(TOKEN_KEY);
      window.localStorage.removeItem(ORG_KEY);
    } catch (e) {
      // storage unavailable: nothing to clear
    }
    return getClient().then(function (client) {
      return client ? client.auth.signOut() : null;
    }).catch(function () {
      return null;
    }).then(function () {
      window.location.href = "/login";
    });
  }

  // Login/logout link (#auth-link). Pages that have it in their HTML get it bound
  // here; the shared shell (shell.js) creates it later and calls this itself.
  function bindAuthLink(link) {
    if (!link || link.getAttribute("data-auth-bound") === "1") {
      return Promise.resolve();
    }
    link.setAttribute("data-auth-bound", "1");
    return getToken().then(function (token) {
      if (token) {
        link.textContent = "로그아웃";
        link.href = "#";
        link.addEventListener("click", function (ev) {
          ev.preventDefault();
          logout();
        });
      } else {
        link.textContent = "로그인";
        link.href = loginUrl();
      }
    });
  }

  window.slAuth = {
    getToken: getToken,
    apiFetch: apiFetch,
    requireLogin: requireLogin,
    logout: logout,
    getClient: getClient,
    bindAuthLink: bindAuthLink
  };

  bindAuthLink(document.getElementById("auth-link"));
})();
