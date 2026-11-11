(function () {
  "use strict";

  var AXIS_LABELS = {
    location: "입지",
    suppliers: "거래처",
    menu: "메뉴",
    pricing: "가격",
    customers: "손님",
    failures: "실패"
  };
  var AXIS_ORDER = ["location", "suppliers", "menu", "pricing", "customers", "failures"];
  var TIMER_LEN = 169.6;
  var DEFAULT_SECONDS = 1200;
  var LOW_SECONDS = 180;

  var parts = window.location.pathname.split("/").filter(function (s) { return s.length > 0; });
  var sessionId = decodeURIComponent(parts[parts.length - 1] || "");

  var el = {
    number: document.getElementById("session-number"),
    remaining: document.getElementById("remaining"),
    itemsToday: document.getElementById("items-today"),
    axis: document.getElementById("axis-label"),
    question: document.getElementById("question-text"),
    audio: document.getElementById("audio-file"),
    transcribe: document.getElementById("transcribe-btn"),
    answer: document.getElementById("answer-text"),
    submit: document.getElementById("submit-btn"),
    end: document.getElementById("end-btn"),
    message: document.getElementById("message"),
    error: document.getElementById("error"),
    reviewLink: document.getElementById("review-link"),
    jobStatus: document.getElementById("stt-job-status"),
    mic: document.getElementById("mic-btn"),
    micStatus: document.getElementById("mic-status"),
    live: document.getElementById("live-transcript"),
    micLabel: document.getElementById("mic-label"),
    altBtn: document.getElementById("alt-input-btn"),
    altInput: document.getElementById("alt-input"),
    axes: document.getElementById("axes"),
    timer: document.querySelector(".timer"),
    timerArc: document.getElementById("timer-arc"),
    view: document.getElementById("interview-view"),
    build: document.getElementById("build"),
    steps: document.querySelectorAll("#gen-steps li")
  };

  var state = { remaining: 0, total: DEFAULT_SECONDS, timerId: null, open: true, busy: false, loaded: false };
  var mic = { active: false, ws: null, stream: null, ctx: null, node: null, pulse: null };

  function motion() {
    return window.GYMotion || null;
  }

  function showAltInput(focus) {
    el.altInput.hidden = false;
    el.altBtn.setAttribute("aria-expanded", "true");
    if (focus) {
      el.answer.focus();
    }
  }

  function textOnly() {
    try {
      return window.sessionStorage.getItem("sl_text_only") === "1";
    } catch (e) {
      return false;
    }
  }

  el.altBtn.addEventListener("click", function () {
    if (el.altInput.hidden) {
      showAltInput(true);
    } else {
      el.altInput.hidden = true;
      el.altBtn.setAttribute("aria-expanded", "false");
    }
  });

  el.audio.addEventListener("change", function () {
    showAltInput(false);
  });

  function buildAxes() {
    el.axes.textContent = "";
    AXIS_ORDER.forEach(function (key) {
      var d = document.createElement("div");
      d.setAttribute("role", "listitem");
      d.setAttribute("data-axis", key);
      var bar = document.createElement("i");
      bar.setAttribute("aria-hidden", "true");
      bar.appendChild(document.createElement("b"));
      var name = document.createElement("span");
      name.textContent = AXIS_LABELS[key];
      d.appendChild(bar);
      d.appendChild(name);
      el.axes.appendChild(d);
    });
  }

  // axis 가 null 이면 여섯 주제를 모두 채운다(질문을 다 마친 상태).
  function setAxes(axis) {
    var idx = axis ? AXIS_ORDER.indexOf(axis) : AXIS_ORDER.length;
    var m = motion();
    Array.prototype.forEach.call(el.axes.children, function (d, i) {
      var cur = i === idx;
      d.classList.toggle("cur", cur);
      if (cur) {
        d.setAttribute("aria-current", "step");
      } else {
        d.removeAttribute("aria-current");
      }
      var ratio = i < idx ? 1 : 0;
      var b = d.querySelector("b");
      if (m) {
        m.fillBar(b, ratio);
      } else {
        b.style.transform = "scaleX(" + ratio + ")";
      }
    });
  }

  function apiUrl(suffix) {
    return "/api/sessions/" + encodeURIComponent(sessionId) + (suffix || "");
  }

  function showError(msg) {
    el.error.textContent = msg;
    el.error.hidden = false;
  }

  function clearError() {
    el.error.textContent = "";
    el.error.hidden = true;
  }

  function showMessage(msg) {
    el.message.textContent = msg;
    el.message.hidden = false;
  }

  function networkMessage(err) {
    var msg = err && err.message ? err.message : "";
    if (!msg || msg.indexOf("Failed to fetch") !== -1) {
      return "서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.";
    }
    return msg;
  }

  function formatTime(seconds) {
    var s = Math.max(0, Math.floor(seconds));
    var mm = Math.floor(s / 60);
    var ss = s % 60;
    return (mm < 10 ? "0" : "") + mm + ":" + (ss < 10 ? "0" : "") + ss;
  }

  function renderRemaining() {
    el.remaining.textContent = formatTime(state.remaining);
    var ratio = state.total > 0 ? Math.max(0, state.remaining) / state.total : 0;
    el.timerArc.setAttribute("stroke-dashoffset", (TIMER_LEN * (1 - ratio)).toFixed(1));
    el.timer.classList.toggle("low", state.remaining <= LOW_SECONDS);
  }

  function startTimer(seconds) {
    state.remaining = seconds;
    state.total = Math.max(state.total, seconds);
    renderRemaining();
    if (state.timerId !== null) {
      window.clearInterval(state.timerId);
      state.timerId = null;
    }
    if (seconds <= 0) {
      return;
    }
    state.timerId = window.setInterval(function () {
      state.remaining -= 1;
      renderRemaining();
      if (state.remaining <= 0) {
        window.clearInterval(state.timerId);
        state.timerId = null;
        showMessage("정해진 시간이 지났어요. 인터뷰를 마치고 카드를 만들어 주세요.");
      }
    }, 1000);
  }

  function setAnswering(enabled) {
    el.submit.disabled = !enabled;
    el.transcribe.disabled = !enabled;
    el.answer.disabled = !enabled;
    el.audio.disabled = !enabled;
    if (!mic.active) {
      el.mic.disabled = !enabled;
    }
  }

  function renderQuestion(question, animate) {
    var apply = function () {
      if (question) {
        var label = AXIS_LABELS[question.axis] || "";
        var pos = AXIS_ORDER.indexOf(question.axis);
        el.axis.textContent = label && pos >= 0 ? label + " · " + (pos + 1) + "/6" : label;
        el.question.textContent = question.text;
      } else {
        el.axis.textContent = "";
        el.question.textContent = "준비한 질문을 모두 마쳤습니다. 인터뷰를 마치고 카드를 만들어 주세요.";
      }
    };
    setAxes(question ? question.axis : null);
    var m = motion();
    if (animate && m) {
      m.swapText([el.axis, el.question], apply);
    } else {
      apply();
    }
  }

  function closeAnswering(msg) {
    state.open = false;
    setAnswering(false);
    if (msg) {
      showMessage(msg);
    }
  }

  function showReviewLink(shopId) {
    if (!shopId) {
      return;
    }
    slAuth.apiFetch("/api/shops/" + encodeURIComponent(shopId) + "/progress")
      .then(function (res) {
        if (!res.ok) {
          return null;
        }
        return res.json();
      })
      .then(function (p) {
        if (p && p.card_id) {
          el.reviewLink.href = "/review/" + encodeURIComponent(p.card_id);
          el.reviewLink.textContent = "카드 검수 화면으로";
          el.reviewLink.hidden = false;
        }
      })
      .catch(function () {
        // 검수 링크는 보조 기능이라 실패해도 화면 흐름을 막지 않는다.
      });
  }

  function load() {
    slAuth.apiFetch(apiUrl(""))
      .then(function (res) {
        if (res.status === 404) {
          throw new Error("인터뷰를 찾지 못했어요. 처음 화면에서 다시 시작해 주세요.");
        }
        if (!res.ok) {
          throw new Error("인터뷰 정보를 불러오지 못했어요. (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (s) {
        el.number.textContent = s.number + "회차 / 예정 " + s.planned + "회";
        el.itemsToday.textContent = String(s.items_today);
        renderQuestion(s.question, false);
        startTimer(s.remaining_seconds);
        state.loaded = true;
        if (s.status !== "open") {
          closeAnswering("이미 마친 인터뷰예요.");
          el.end.disabled = true;
          showReviewLink(s.shop_id);
        } else if (!s.question) {
          closeAnswering("");
        } else if (window.slShell) {
          window.slShell.coach(el.mic.disabled ? el.answer : el.mic);
        }
      })
      .catch(function (err) {
        el.question.textContent = "질문을 불러오지 못했어요.";
        showError(networkMessage(err));
      });
  }

  var POLL_MS = 2000;
  var FIRST_POLL_MS = 500;

  function setJobStatus(msg) {
    el.jobStatus.textContent = msg;
  }

  function jobFinished() {
    if (state.open) {
      el.transcribe.disabled = false;
    }
  }

  function pollJob(jobId, delay) {
    window.setTimeout(function () {
      slAuth.apiFetch("/api/stt/jobs/" + encodeURIComponent(jobId))
        .then(function (res) {
          if (!res.ok) {
            throw new Error("작업 상태를 확인하지 못했어요. (" + res.status + ")");
          }
          return res.json();
        })
        .then(function (job) {
          if (job.status === "done") {
            el.answer.value = job.text || "";
            showAltInput(false);
            setJobStatus("완료");
            jobFinished();
          } else if (job.status === "failed") {
            setJobStatus("");
            showError(job.error || "녹음을 글로 옮기지 못했어요.");
            jobFinished();
          } else {
            pollJob(jobId, POLL_MS);
          }
        })
        .catch(function (err) {
          setJobStatus("");
          showError(networkMessage(err));
          jobFinished();
        });
    }, delay);
  }

  el.transcribe.addEventListener("click", function () {
    clearError();
    var file = el.audio.files && el.audio.files[0];
    if (!file) {
      showError("먼저 녹음 파일을 골라 주세요.");
      return;
    }
    var data = new FormData();
    data.append("file", file, file.name);
    data.append("session_id", sessionId);
    el.transcribe.disabled = true;
    el.message.hidden = true;
    setJobStatus("받아 적는 중…");
    slAuth.apiFetch("/api/stt/jobs", { method: "POST", body: data })
      .then(function (res) {
        if (res.status === 415) {
          throw new Error("지원하지 않는 파일 형식이에요. wav, mp3, m4a, webm, ogg 파일을 올려 주세요.");
        }
        if (res.status === 413) {
          throw new Error("파일이 너무 커요. 더 짧은 녹음으로 나눠 올려 주세요.");
        }
        if (res.status === 404) {
          throw new Error("인터뷰를 찾지 못했어요.");
        }
        if (!res.ok) {
          throw new Error("녹음을 글로 옮기지 못했어요. (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (job) {
        if (job.status === "done") {
          el.answer.value = job.text || "";
          showAltInput(false);
          setJobStatus("완료");
          jobFinished();
        } else {
          pollJob(job.job_id, FIRST_POLL_MS);
        }
      })
      .catch(function (err) {
        setJobStatus("");
        showError(networkMessage(err));
        jobFinished();
      });
  });

  function setMicStatus(msg) {
    el.micStatus.textContent = msg;
  }

  function setMicButton(active) {
    mic.active = active;
    el.mic.setAttribute("aria-pressed", active ? "true" : "false");
    el.micLabel.textContent = active ? "말하기 멈추기" : "마이크로 말하기";
    el.mic.classList.toggle("rec", active);
    if (active && !mic.pulse && motion()) {
      mic.pulse = motion().micPulse(el.mic.querySelectorAll(".ring"));
    } else if (!active && mic.pulse) {
      mic.pulse.kill();
      mic.pulse = null;
    }
  }

  function releaseAudio() {
    if (mic.node) {
      mic.node.port.onmessage = null;
      mic.node.disconnect();
      mic.node = null;
    }
    if (mic.stream) {
      mic.stream.getTracks().forEach(function (t) { t.stop(); });
      mic.stream = null;
    }
    if (mic.ctx) {
      mic.ctx.close().catch(function () {});
      mic.ctx = null;
    }
  }

  function appendFinal(text) {
    var t = (text || "").trim();
    if (!t) {
      return;
    }
    var p = document.createElement("p");
    p.textContent = t;
    el.live.appendChild(p);
    var current = el.answer.value;
    el.answer.value = current ? current + " " + t : t;
  }

  function streamUrl() {
    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + window.location.host + "/api/stt/stream";
  }

  function micFailed(msg) {
    releaseAudio();
    if (mic.ws) {
      var ws = mic.ws;
      mic.ws = null;
      try { ws.close(); } catch (e) { /* already closed */ }
    }
    setMicButton(false);
    setMicStatus("");
    el.mic.disabled = !state.open;
    showError(msg);
  }

  function startMic() {
    clearError();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.AudioWorkletNode) {
      showError("이 브라우저에서는 마이크를 쓸 수 없어요. 녹음 파일을 올려 주세요.");
      return;
    }
    el.mic.disabled = true;
    setMicStatus("마이크 준비 중…");
    var tokenP = slAuth.getToken();
    navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
      .then(function (stream) {
        mic.stream = stream;
        mic.ctx = new AudioContext();
        return mic.ctx.audioWorklet.addModule("/static/pcm-worklet.js");
      })
      .then(function () {
        return tokenP;
      })
      .then(function (token) {
        if (!token) {
          throw new Error("로그인이 필요해요.");
        }
        var ws = new WebSocket(streamUrl());
        ws.binaryType = "arraybuffer";
        mic.ws = ws;
        ws.onopen = function () {
          ws.send(JSON.stringify({ type: "start", token: token, session_id: sessionId, sample_rate: 16000 }));
        };
        ws.onmessage = function (ev) {
          if (typeof ev.data !== "string") {
            return;
          }
          var msg;
          try { msg = JSON.parse(ev.data); } catch (e) { return; }
          if (msg.type === "ready") {
            var source = mic.ctx.createMediaStreamSource(mic.stream);
            mic.node = new AudioWorkletNode(mic.ctx, "pcm16-worklet");
            mic.node.port.onmessage = function (e) {
              if (mic.ws && mic.ws.readyState === WebSocket.OPEN) {
                mic.ws.send(e.data);
              }
            };
            source.connect(mic.node);
            setMicButton(true);
            el.mic.disabled = false;
            setMicStatus("듣는 중");
          } else if (msg.type === "final") {
            appendFinal(msg.text);
          } else if (msg.type === "done") {
            releaseAudio();
            setMicButton(false);
            el.mic.disabled = !state.open;
            setMicStatus("끝");
            if (el.answer.value) {
              showAltInput(false);
            }
          }
        };
        ws.onclose = function (ev) {
          if (mic.ws !== ws) {
            return;
          }
          mic.ws = null;
          var wasListening = mic.active || el.mic.disabled;
          releaseAudio();
          setMicButton(false);
          el.mic.disabled = !state.open;
          if (ev.code !== 1000 && wasListening) {
            setMicStatus("");
            showError("음성 연결이 끊겼어요. (" + ev.code + ")");
          }
        };
      })
      .catch(function (err) {
        var msg = err && err.name === "NotAllowedError"
          ? "마이크 사용을 허락해 주세요."
          : networkMessage(err);
        micFailed(msg);
      });
  }

  function stopMic() {
    if (mic.node) {
      mic.node.port.onmessage = null;
    }
    if (mic.ws && mic.ws.readyState === WebSocket.OPEN) {
      el.mic.disabled = true;
      setMicStatus("마무리 중…");
      mic.ws.send(JSON.stringify({ type: "stop" }));
    } else {
      releaseAudio();
      setMicButton(false);
      setMicStatus("끝");
    }
  }

  el.mic.addEventListener("click", function () {
    if (mic.active) {
      stopMic();
    } else if (state.open) {
      startMic();
    }
  });

  el.submit.addEventListener("click", function () {
    if (state.busy || !state.open) {
      return;
    }
    clearError();
    var text = el.answer.value.trim();
    if (!text) {
      showError("답변을 입력해 주세요.");
      return;
    }
    state.busy = true;
    el.submit.disabled = true;
    slAuth.apiFetch(apiUrl("/answer"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text })
    })
      .then(function (res) {
        if (res.status === 410) {
          closeAnswering("");
          throw new Error("정해진 시간이 지나 답변을 더 받을 수 없어요. 인터뷰를 마치고 카드를 만들어 주세요.");
        }
        if (res.status === 409) {
          closeAnswering("");
          throw new Error("이미 마친 인터뷰예요. 답변을 더 받을 수 없어요.");
        }
        if (res.status === 422) {
          throw new Error("답변 내용을 확인해 주세요.");
        }
        if (!res.ok) {
          throw new Error("답변을 보내지 못했어요. (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (r) {
        el.itemsToday.textContent = String(r.items_today);
        startTimer(r.remaining_seconds);
        el.answer.value = "";
        el.audio.value = "";
        el.message.hidden = true;
        el.live.textContent = "";
        renderQuestion(r.next_question, true);
        if (!r.next_question) {
          closeAnswering("");
        }
      })
      .catch(function (err) {
        showError(networkMessage(err));
      })
      .then(function () {
        state.busy = false;
        if (state.open) {
          el.submit.disabled = false;
        }
      });
  });

  var STEP_MS = 700;
  var FINISH_MS = 150;
  var build = { timer: null, done: 0 };

  function checkStep(i) {
    var li = el.steps[i];
    if (!li || li.classList.contains("on")) {
      return;
    }
    li.classList.add("on");
    build.done = Math.max(build.done, i + 1);
    var g = window.gsap;
    var m = motion();
    if (g && m && !m.reduced) {
      g.fromTo(li.querySelector(".ck"), { scale: 0.6 }, { scale: 1, duration: 0.4, ease: "back.out(3)" });
    }
  }

  function showBuild() {
    Array.prototype.forEach.call(el.steps, function (li) { li.classList.remove("on"); });
    build.done = 0;
    el.view.hidden = true;
    el.build.hidden = false;
    var m = motion();
    if (m) {
      m.enterPage(el.build, 1);
    }
    // 서버가 답을 주기 전에는 마지막 단계(근거)를 체크하지 않는다.
    checkStep(0);
    build.timer = window.setInterval(function () {
      if (build.done < el.steps.length - 1) {
        checkStep(build.done);
      }
    }, STEP_MS);
  }

  function hideBuild() {
    if (build.timer !== null) {
      window.clearInterval(build.timer);
      build.timer = null;
    }
    el.build.hidden = true;
    el.view.hidden = false;
  }

  function finishBuild(href) {
    if (build.timer !== null) {
      window.clearInterval(build.timer);
      build.timer = null;
    }
    var next = function () {
      if (build.done < el.steps.length) {
        checkStep(build.done);
        window.setTimeout(next, FINISH_MS);
      } else {
        window.setTimeout(function () { window.location.href = href; }, FINISH_MS);
      }
    };
    next();
  }

  el.end.addEventListener("click", function () {
    clearError();
    el.end.disabled = true;
    el.message.hidden = true;
    showBuild();
    slAuth.apiFetch(apiUrl("/end"), { method: "POST" })
      .then(function (res) {
        if (res.status === 409) {
          throw new Error("이미 마친 인터뷰예요.");
        }
        if (res.status === 404) {
          throw new Error("인터뷰를 찾지 못했어요.");
        }
        if (!res.ok) {
          throw new Error("인터뷰를 마치지 못했어요. (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (r) {
        finishBuild("/review/" + encodeURIComponent(r.card_id));
      })
      .catch(function (err) {
        hideBuild();
        showError(networkMessage(err));
        el.end.disabled = false;
      });
  });

  if (window.slA11y) {
    window.slA11y.init();
    var ttsBtn = document.getElementById("tts-btn");
    if (ttsBtn) {
      ttsBtn.addEventListener("click", function () {
        var axisText = el.axis.textContent.trim();
        window.slA11y.speak((axisText ? axisText + ". " : "") + el.question.textContent);
      });
    }
  }

  buildAxes();
  setMicButton(false);
  if (textOnly()) {
    showAltInput(false);
  }
  if (motion()) {
    motion().enterPage();
  }
  load();
})();
