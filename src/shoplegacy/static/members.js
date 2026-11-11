// Org members: list, add by e-mail (someone who has signed in once) and remove.
// Admin or owner only; the API answers 403 to everyone else.
(function () {
  "use strict";

  var ORG_KEY = "sl_org_id";
  var ROLE_LABELS = {
    interviewer: "인터뷰어",
    reviewer: "검토자",
    admin: "관리자",
    owner: "소유자"
  };

  var el = {
    pageError: document.getElementById("members-error"),
    addBox: document.getElementById("member-add-box"),
    email: document.getElementById("member-email"),
    role: document.getElementById("member-role"),
    add: document.getElementById("member-add"),
    addError: document.getElementById("member-error"),
    list: document.getElementById("member-list"),
    count: document.getElementById("member-count"),
    loading: document.getElementById("members-loading")
  };

  var myRole = null;
  var coached = false;

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
    return "/api/orgs/" + encodeURIComponent(orgId() || "") + "/members";
  }

  function readJson(res) {
    return res.json().catch(function () {
      return {};
    });
  }

  function toast(msg) {
    var s = shell();
    if (s && s.toast) {
      s.toast(msg);
    }
  }

  function showPageError(msg) {
    el.pageError.textContent = msg;
    el.pageError.hidden = false;
  }

  function showAddError(msg) {
    el.addError.textContent = msg;
    el.addError.hidden = false;
  }

  function clearAddError() {
    el.addError.textContent = "";
    el.addError.hidden = true;
  }

  function errorText(status, detail) {
    if (status === 404 && detail === "user not found") {
      return "이 이메일로 로그인한 사람을 찾지 못했어요. 먼저 한 번 로그인해 달라고 알려 주세요.";
    }
    if (status === 402) {
      return "구성원 한도에 닿았어요. 요금제를 확인해 주세요.";
    }
    if (status === 403) {
      return "이 구성원을 바꿀 권한이 없어요. 소유자는 소유자만 바꿀 수 있어요.";
    }
    if (status === 409 && detail === "last_owner") {
      return "소유자가 한 명뿐이라 내보낼 수 없어요. 다른 소유자를 먼저 추가해 주세요.";
    }
    if (status === 422) {
      return "이메일 주소를 다시 확인해 주세요.";
    }
    return "처리하지 못했어요. 잠시 후 다시 시도해 주세요. (" + status + ")";
  }

  function render(members) {
    el.list.textContent = "";
    el.count.textContent = members.length + "명";
    members.forEach(function (m) {
      var li = document.createElement("li");
      li.className = "mem";
      li.setAttribute("data-user-id", m.user_id);

      var av = document.createElement("span");
      av.className = "av";
      av.setAttribute("aria-hidden", "true");
      av.textContent = (m.email || "?").charAt(0).toUpperCase();

      var who = document.createElement("span");
      who.className = "who";
      var email = document.createElement("b");
      email.className = "member-email";
      email.textContent = m.email || "(이메일 없음)";
      var role = document.createElement("span");
      role.className = "member-role-label hint";
      role.textContent = ROLE_LABELS[m.role] || m.role;
      who.appendChild(email);
      who.appendChild(role);

      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn quiet member-remove";
      remove.textContent = "내보내기";
      remove.setAttribute("aria-label", (m.email || "구성원") + " 내보내기");

      // Inline confirm: the DELETE only goes out after "빼기" is pressed.
      var confirmBox = document.createElement("div");
      confirmBox.className = "member-confirm";
      confirmBox.hidden = true;
      var ask = document.createElement("span");
      ask.className = "member-confirm-ask";
      ask.textContent = "정말 뺄까요?";
      var yes = document.createElement("button");
      yes.type = "button";
      yes.className = "btn member-remove-confirm";
      yes.textContent = "빼기";
      yes.setAttribute("aria-label", (m.email || "구성원") + " 빼기");
      var no = document.createElement("button");
      no.type = "button";
      no.className = "btn quiet member-remove-cancel";
      no.textContent = "취소";
      confirmBox.appendChild(ask);
      confirmBox.appendChild(yes);
      confirmBox.appendChild(no);

      remove.addEventListener("click", function () {
        remove.hidden = true;
        confirmBox.hidden = false;
        no.focus();
      });
      no.addEventListener("click", function () {
        confirmBox.hidden = true;
        remove.hidden = false;
        remove.focus();
      });
      yes.addEventListener("click", function () {
        no.disabled = true;
        removeMember(m, yes, function () {
          no.disabled = false;
        });
      });

      li.appendChild(av);
      li.appendChild(who);
      li.appendChild(remove);
      li.appendChild(confirmBox);
      el.list.appendChild(li);
    });
  }

  function load() {
    return window.slAuth.apiFetch(apiBase()).then(function (res) {
      return readJson(res).then(function (body) {
        el.loading.hidden = true;
        if (res.status === 403) {
          el.addBox.hidden = true;
          showPageError("구성원은 기관 관리자와 소유자만 볼 수 있어요.");
          return;
        }
        if (!res.ok) {
          showPageError("구성원을 불러오지 못했어요. 잠시 후 다시 시도해 주세요. (" + res.status + ")");
          return;
        }
        render(Array.isArray(body) ? body : []);
        if (!coached) {
          coached = true;
          var s = shell();
          if (s && s.coach) {
            // Wait for the page enter slide so the ring is measured in place.
            window.setTimeout(function () {
              s.coach(el.email);
            }, 500);
          }
        }
      });
    });
  }

  function addMember() {
    clearAddError();
    var email = el.email.value.trim();
    if (!email) {
      showAddError("추가할 사람의 이메일을 입력해 주세요.");
      el.email.focus();
      return;
    }
    el.add.disabled = true;
    window.slAuth.apiFetch(apiBase(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email, role: el.role.value })
    }).then(function (res) {
      return readJson(res).then(function (body) {
        if (!res.ok) {
          showAddError(errorText(res.status, body && body.detail));
          return null;
        }
        el.email.value = "";
        toast((body.email || email) + " 님을 " + (ROLE_LABELS[body.role] || body.role) + "(으)로 추가했어요.");
        return load();
      });
    }).catch(function () {
      showAddError("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.");
    }).then(function () {
      el.add.disabled = false;
    });
  }

  function removeMember(m, btn, onFail) {
    clearAddError();
    btn.disabled = true;
    function failed() {
      btn.disabled = false;
      if (onFail) {
        onFail();
      }
    }
    window.slAuth.apiFetch(apiBase() + "/" + encodeURIComponent(m.user_id), {
      method: "DELETE"
    }).then(function (res) {
      if (!res.ok) {
        return readJson(res).then(function (body) {
          showAddError(errorText(res.status, body && body.detail));
          failed();
        });
      }
      toast((m.email || "구성원") + " 님을 내보냈어요.");
      return load();
    }).catch(function () {
      failed();
      showAddError("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.");
    });
  }

  el.add.addEventListener("click", addMember);
  el.email.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") {
      ev.preventDefault();
      addMember();
    }
  });

  function start() {
    var s = shell();
    var ready = s && s.ready ? s.ready : Promise.resolve({ role: null });
    return window.slAuth.requireLogin().then(function () {
      return ready;
    }).then(function (info) {
      myRole = info && info.role;
      if (myRole !== "owner") {
        // Only an owner may hand out the owner role.
        var opt = el.role.querySelector('option[value="owner"]');
        if (opt) {
          opt.remove();
        }
      }
      if (!orgId()) {
        el.loading.hidden = true;
        showPageError("기관 정보를 찾지 못했어요. 다시 로그인해 주세요.");
        return null;
      }
      return load();
    });
  }

  start().catch(function () {
    el.loading.hidden = true;
    showPageError("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.");
  });

  document.addEventListener("DOMContentLoaded", function () {
    if (window.GYMotion) {
      window.GYMotion.enterPage();
    }
  });
})();
