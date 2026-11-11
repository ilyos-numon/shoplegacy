// Shared accessibility helpers: large-type toggle and read-aloud.
(function () {
  "use strict";

  var STORAGE_KEY = "sl_large_type";
  var LARGE_CLASS = "large-type";

  function readPref() {
    try {
      return window.localStorage.getItem(STORAGE_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function writePref(on) {
    try {
      if (on) {
        window.localStorage.setItem(STORAGE_KEY, "1");
      } else {
        window.localStorage.removeItem(STORAGE_KEY);
      }
    } catch (e) {
      // 저장이 막힌 브라우저에서는 이번 화면에만 적용한다.
    }
  }

  function canSpeak() {
    return "speechSynthesis" in window && typeof window.SpeechSynthesisUtterance === "function";
  }

  function applyLargeType(on, toggle) {
    document.documentElement.classList.toggle(LARGE_CLASS, on);
    if (toggle) {
      toggle.setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  function pickKoreanVoice() {
    var voices = window.speechSynthesis.getVoices() || [];
    for (var i = 0; i < voices.length; i += 1) {
      if (voices[i].lang && voices[i].lang.toLowerCase().indexOf("ko") === 0) {
        return voices[i];
      }
    }
    return null;
  }

  function speak(text) {
    if (!canSpeak() || !text) {
      return false;
    }
    var synth = window.speechSynthesis;
    synth.cancel();
    var utter = new window.SpeechSynthesisUtterance(text);
    utter.lang = "ko-KR";
    utter.rate = 0.9;
    var voice = pickKoreanVoice();
    if (voice) {
      utter.voice = voice;
    }
    synth.speak(utter);
    return true;
  }

  function init() {
    var toggle = document.getElementById("large-type-toggle");
    applyLargeType(readPref(), toggle);
    if (toggle && !toggle.dataset.slBound) {
      toggle.dataset.slBound = "1";
      toggle.addEventListener("click", function () {
        var on = !document.documentElement.classList.contains(LARGE_CLASS);
        applyLargeType(on, toggle);
        writePref(on);
      });
    }
    var tts = document.getElementById("tts-btn");
    if (tts && !canSpeak()) {
      tts.hidden = true;
    }
  }

  window.slA11y = { init: init, speak: speak, canSpeak: canSpeak };
})();
