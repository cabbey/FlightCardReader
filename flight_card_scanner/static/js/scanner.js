/**
 * scanner.js — Flight Card Scanner client-side logic
 *
 * Handles camera access, switching, detection pipeline (future),
 * perspective transform (future), and confirmation/submission (future).
 */
(function () {
  'use strict';

  // =========================================================================
  // Tunable Constants (detection pipeline — used by future tasks)
  // =========================================================================

  /** Minimum card-area / frame-area ratio for a valid detection */
  var MIN_FILL = 0.15;

  /** Max corner displacement as a fraction of frame width (0.5% = very steady hand-hold) */
  var STABILITY_THRESHOLD_RATIO = 0.005;

  /** Consecutive stable frames required before auto-capture */
  var STABILITY_FRAMES = 5;

  /** Laplacian variance threshold for focus check */
  var FOCUS_THRESHOLD = 20.0;

  /** Minimum output width (px) after perspective correction */
  var OUTPUT_W = 1000;

  /** Minimum output height (px) after perspective correction */
  var OUTPUT_H = 1300;

  // =========================================================================
  // Module-level State
  // =========================================================================

  /** @type {HTMLDivElement|null} Debug overlay element */
  var debugOverlayEl = null;

  /**
   * Log a scanner debug message to console and the debug overlay.
   * @param {string} msg
   */
  function debugLog(msg) {
    console.log(msg);
    if (debugOverlayEl) {
      // Strip the [Scanner] prefix for the on-screen display
      var display = msg.replace(/^\[Scanner\]\s*/, '');
      debugOverlayEl.textContent = display;
    }
  }

  /** @type {MediaDeviceInfo[]} List of available video input devices */
  var cameras = [];

  /** @type {number} Index into the cameras array for the active camera */
  var currentCameraIndex = -1;

  /** @type {MediaStream|null} The active camera stream */
  var currentStream = null;

  /** @type {HTMLVideoElement} */
  var videoEl = null;

  /** @type {HTMLCanvasElement} */
  var overlayEl = null;

  /** @type {HTMLButtonElement} */
  var switchBtn = null;

  /** @type {HTMLDivElement} */
  var errorPermissionEl = null;

  /** @type {HTMLDivElement} */
  var errorUnsupportedEl = null;

  // Confirmation screen elements
  /** @type {HTMLDivElement} */
  var livePreviewStateEl = null;

  /** @type {HTMLDivElement} */
  var confirmationStateEl = null;

  /** @type {HTMLImageElement} */
  var capturePreviewEl = null;

  /** @type {HTMLButtonElement} */
  var acceptBtn = null;

  /** @type {HTMLButtonElement} */
  var rejectBtn = null;

  /** @type {HTMLDivElement} */
  var spinnerOverlayEl = null;

  /** @type {HTMLDivElement} */
  var toastSuccessEl = null;

  /** @type {HTMLDivElement} */
  var toastErrorEl = null;

  /** @type {string|null} Current captured JPEG data URL */
  var capturedDataUrl = null;

  /** @type {number|null} Touch start Y coordinate for swipe gesture */
  var swipeTouchStartY = null;

  // =========================================================================
  // Detection Pipeline State
  // =========================================================================

  /** @type {HTMLCanvasElement|null} Offscreen canvas for frame processing */
  var offscreenCanvas = null;

  /** @type {CanvasRenderingContext2D|null} Offscreen canvas context */
  var offscreenCtx = null;

  /** @type {boolean} Whether OpenCV.js is ready */
  var cvReady = false;

  /** @type {boolean} Whether the detection loop is running */
  var detectionRunning = false;

  /** @type {number|null} requestAnimationFrame ID */
  var rafId = null;

  /**
   * Previous frame's detected corners (array of 4 {x,y} objects), or null
   * @type {Array<{x: number, y: number}>|null}
   */
  var previousCorners = null;

  /** @type {number} Count of consecutive stable frames */
  var stableFrameCount = 0;

  // =========================================================================
  // Camera Enumeration
  // =========================================================================

  /**
   * Enumerate available video input devices.
   * Must be called after at least one getUserMedia call has succeeded,
   * otherwise device labels may be empty.
   *
   * @returns {Promise<MediaDeviceInfo[]>}
   */
  async function enumerateCameras() {
    var devices = await navigator.mediaDevices.enumerateDevices();
    cameras = devices.filter(function (d) {
      return d.kind === 'videoinput';
    });
    return cameras;
  }

  // =========================================================================
  // Camera Start / Stop
  // =========================================================================

  /**
   * Stop all tracks on the current stream, if any.
   */
  function stopCurrentStream() {
    if (currentStream) {
      currentStream.getTracks().forEach(function (track) {
        track.stop();
      });
      currentStream = null;
    }
  }

  /**
   * Start (or restart) the camera.
   *
   * @param {string|null} deviceId - Specific device ID, or null to use
   *   environment-facing camera (rear) as default.
   * @returns {Promise<void>}
   */
  async function startCamera(deviceId) {
    stopCurrentStream();

    var constraints;

    if (deviceId) {
      // Explicit device requested (e.g. from switchCamera)
      constraints = {
        video: {
          deviceId: { exact: deviceId },
          width: { ideal: 3840 },
          height: { ideal: 2160 }
        },
        audio: false
      };
    } else {
      // First call — prefer environment-facing (rear) camera
      // Request high resolution for quality card images
      constraints = {
        video: {
          facingMode: { ideal: 'environment' },
          width: { ideal: 3840 },
          height: { ideal: 2160 }
        },
        audio: false
      };
    }

    try {
      currentStream = await navigator.mediaDevices.getUserMedia(constraints);
      videoEl.srcObject = currentStream;

      // iOS Safari requires an explicit play() call
      // and a loadedmetadata listener before video renders
      await new Promise(function (resolve, reject) {
        videoEl.onloadedmetadata = function () {
          videoEl.play().then(resolve).catch(resolve);
        };
        // Fallback timeout in case loadedmetadata never fires
        setTimeout(resolve, 3000);
      });

      // After first successful getUserMedia, enumerate to get labels
      await enumerateCameras();

      // Determine the current camera index from the active track
      var activeTrack = currentStream.getVideoTracks()[0];
      if (activeTrack) {
        var settings = activeTrack.getSettings();
        var activeDeviceId = settings.deviceId;
        for (var i = 0; i < cameras.length; i++) {
          if (cameras[i].deviceId === activeDeviceId) {
            currentCameraIndex = i;
            break;
          }
        }
        // Log camera resolution
        var camW = settings.width || videoEl.videoWidth;
        var camH = settings.height || videoEl.videoHeight;
        // If dimensions still not available, wait one more frame
        if (camW === 0 || camH === 0) {
          await new Promise(function(resolve) { requestAnimationFrame(resolve); });
          camW = videoEl.videoWidth;
          camH = videoEl.videoHeight;
        }
        debugLog('[Scanner] Camera started: ' + camW + 'x' + camH +
          ' (device: ' + (activeTrack.label || activeDeviceId) + ')');

        // Set preview container aspect ratio to match camera
        if (camW > 0 && camH > 0) {
          var wrapper = videoEl.closest('.preview-wrapper');
          if (wrapper) {
            var aspectRatio = camW / camH;
            wrapper.style.aspectRatio = aspectRatio.toString();
            debugLog('[Scanner] Container aspect ratio set to ' + aspectRatio.toFixed(3));
          }
        }

        // Check if camera resolution is sufficient
        if (camW < OUTPUT_W || camH < OUTPUT_H) {
          debugLog('[Scanner] WARNING: Camera resolution ' + camW + 'x' + camH +
            ' is below minimum ' + OUTPUT_W + 'x' + OUTPUT_H +
            '. Auto-capture will never trigger. Use a higher-resolution camera.');
          showResolutionWarning(camW, camH);
        } else {
          debugLog('[Scanner] Camera resolution is sufficient for auto-capture ' +
            '(min: ' + OUTPUT_W + 'x' + OUTPUT_H + ')');
          hideResolutionWarning();
        }
      }
    } catch (err) {
      // On iOS, if facingMode constraint fails, retry with basic video: true
      if (!deviceId && err.name === 'OverconstrainedError') {
        try {
          currentStream = await navigator.mediaDevices.getUserMedia({
            video: { width: { ideal: 3840 }, height: { ideal: 2160 } },
            audio: false
          });
          videoEl.srcObject = currentStream;
          await new Promise(function (resolve) {
            videoEl.onloadedmetadata = function () {
              videoEl.play().then(resolve).catch(resolve);
            };
            setTimeout(resolve, 3000);
          });
          await enumerateCameras();
        } catch (retryErr) {
          handleCameraError(retryErr);
        }
      } else {
        handleCameraError(err);
      }
    }
  }

  // =========================================================================
  // Camera Switching
  // =========================================================================

  /**
   * Cycle to the next available camera and start it.
   *
   * @returns {Promise<void>}
   */
  async function switchCamera() {
    if (cameras.length < 2) {
      // No point switching with 0 or 1 camera
      return;
    }

    currentCameraIndex = (currentCameraIndex + 1) % cameras.length;
    var nextDevice = cameras[currentCameraIndex];
    await startCamera(nextDevice.deviceId);
  }

  // =========================================================================
  // Error Handling
  // =========================================================================

  /**
   * Display the appropriate error overlay based on the error type.
   *
   * @param {Error} err
   */
  function handleCameraError(err) {
    var name = err.name || '';

    if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
      showError(errorPermissionEl);
    } else if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
      // No camera hardware available — show permission error as fallback
      showError(errorPermissionEl);
    } else {
      // For other errors (OverconstrainedError, NotReadableError, etc.)
      // show permission overlay as a generic fallback
      showError(errorPermissionEl);
    }
  }

  /**
   * Show an error overlay by adding the "active" class.
   *
   * @param {HTMLElement} el
   */
  function showError(el) {
    if (el) {
      el.classList.add('active');
    }
  }

  /**
   * Show the unsupported browser error overlay.
   */
  function showUnsupportedError() {
    showError(errorUnsupportedEl);
  }

  /**
   * Show a warning that camera resolution is too low for auto-capture.
   *
   * @param {number} w - Camera width
   * @param {number} h - Camera height
   */
  function showResolutionWarning(w, h) {
    var existing = document.getElementById('resolutionWarning');
    if (existing) {
      existing.style.display = 'block';
      return;
    }
    var warning = document.createElement('div');
    warning.id = 'resolutionWarning';
    warning.style.cssText = 'background:#fef3c7;color:#92400e;padding:0.6rem 1rem;border-radius:4px;font-size:0.85rem;margin-top:0.5rem;text-align:center;';
    warning.textContent = 'Camera resolution (' + w + '×' + h +
      ') is too low for auto-capture (need ' + OUTPUT_W + '×' + OUTPUT_H +
      '). Try a different camera or move the device.';
    var controls = document.querySelector('.controls');
    if (controls) {
      controls.parentNode.insertBefore(warning, controls.nextSibling);
    }
  }

  /**
   * Hide the resolution warning.
   */
  function hideResolutionWarning() {
    var existing = document.getElementById('resolutionWarning');
    if (existing) {
      existing.style.display = 'none';
    }
  }

  // =========================================================================
  // Detection Pipeline — Constants for downsampled processing
  // =========================================================================

  /** Target width for the detection frame (downsampled for performance) */
  var DETECT_W = 960;

  // =========================================================================
  // OpenCV.js Detection Pipeline
  // =========================================================================

  /**
   * Capture and process a single video frame through the OpenCV.js pipeline.
   * The frame is downsampled to DETECT_W for faster processing, then detected
   * corners are scaled back to full video resolution.
   *
   * Steps: draw video → downsample → grayscale → blur → Canny → findContours →
   * approxPolyDP → select largest 4-vertex contour → area check → scale up.
   *
   * @returns {{corners: Array<{x: number, y: number}>}|null} Detected card
   *   corners (in full video coordinates) or null if no valid card found.
   */
  function captureFrame() {
    if (!cvReady || !videoEl || videoEl.readyState < 2) {
      return null;
    }

    var vw = videoEl.videoWidth;
    var vh = videoEl.videoHeight;
    if (vw === 0 || vh === 0) {
      return null;
    }

    // Compute downsampled dimensions (maintain aspect ratio)
    var scale = (vw > DETECT_W) ? (DETECT_W / vw) : 1.0;
    var dw = Math.round(vw * scale);
    var dh = Math.round(vh * scale);

    // Ensure offscreen canvas matches full video dimensions (for perspective transform later)
    if (!offscreenCanvas) {
      offscreenCanvas = document.createElement('canvas');
      offscreenCtx = offscreenCanvas.getContext('2d');
    }
    if (offscreenCanvas.width !== vw || offscreenCanvas.height !== vh) {
      offscreenCanvas.width = vw;
      offscreenCanvas.height = vh;
    }

    // Draw full-resolution video frame to offscreen canvas
    offscreenCtx.drawImage(videoEl, 0, 0, vw, vh);

    // Create a downsampled canvas for detection
    var detectCanvas = document.createElement('canvas');
    detectCanvas.width = dw;
    detectCanvas.height = dh;
    var detectCtx = detectCanvas.getContext('2d');
    detectCtx.drawImage(offscreenCanvas, 0, 0, dw, dh);

    // OpenCV Mat from downsampled canvas
    var src = cv.imread(detectCanvas);
    var gray = new cv.Mat();
    var blurred = new cv.Mat();
    var edges = new cv.Mat();
    var contours = new cv.MatVector();
    var hierarchy = new cv.Mat();

    try {
      // Convert to grayscale
      cv.cvtColor(src, gray, cv.COLOR_RGBA2GRAY);

      // Gaussian blur — 7×7 works well at ~960px width
      var ksize = new cv.Size(7, 7);
      cv.GaussianBlur(gray, blurred, ksize, 0);

      // Canny edge detection (tuned for downsampled frame)
      cv.Canny(blurred, edges, 50, 150);

      // Dilate edges slightly to close gaps
      var dilateKernel = cv.Mat.ones(3, 3, cv.CV_8U);
      cv.dilate(edges, edges, dilateKernel);
      dilateKernel.delete();

      // Find external contours
      cv.findContours(edges, contours, hierarchy, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE);

      var frameArea = dw * dh;
      var bestContour = null;
      var bestArea = 0;

      // Iterate contours, find largest 4-vertex polygon
      for (var i = 0; i < contours.size(); i++) {
        var contour = contours.get(i);
        var perimeter = cv.arcLength(contour, true);
        var approx = new cv.Mat();

        // approxPolyDP with epsilon = 0.02 * perimeter
        cv.approxPolyDP(contour, approx, 0.02 * perimeter, true);

        if (approx.rows === 4) {
          var area = cv.contourArea(approx);
          if (area > bestArea) {
            if (bestContour) {
              bestContour.delete();
            }
            bestContour = approx;
            bestArea = area;
          } else {
            approx.delete();
          }
        } else {
          approx.delete();
        }
      }

      // Area check: contour area / frame area ≥ MIN_FILL
      if (bestContour && (bestArea / frameArea) >= MIN_FILL) {
        // Extract corner points and scale back to full resolution
        var invScale = 1.0 / scale;
        var corners = [];
        for (var j = 0; j < 4; j++) {
          corners.push({
            x: Math.round(bestContour.data32S[j * 2] * invScale),
            y: Math.round(bestContour.data32S[j * 2 + 1] * invScale)
          });
        }
        bestContour.delete();
        return { corners: corners };
      }

      if (bestContour) {
        bestContour.delete();
      }
      return null;
    } finally {
      src.delete();
      gray.delete();
      blurred.delete();
      edges.delete();
      contours.delete();
      hierarchy.delete();
    }
  }

  /**
   * Check if detected corners are stable compared to previous frame.
   * Compares max corner displacement between current and previous corners.
   * Threshold is proportional to video width for resolution independence.
   *
   * @param {Array<{x: number, y: number}>} corners - Current frame corners
   * @returns {boolean} True if card has been stable long enough
   */
  function stabilityCheck(corners) {
    if (!previousCorners) {
      previousCorners = corners;
      stableFrameCount = 1;
      return false;
    }

    // Compute threshold based on video width
    var threshold = videoEl.videoWidth * STABILITY_THRESHOLD_RATIO;

    // Compute max displacement across all corners
    var maxDisplacement = 0;
    for (var i = 0; i < 4; i++) {
      var dx = corners[i].x - previousCorners[i].x;
      var dy = corners[i].y - previousCorners[i].y;
      var dist = Math.sqrt(dx * dx + dy * dy);
      if (dist > maxDisplacement) {
        maxDisplacement = dist;
      }
    }

    previousCorners = corners;

    if (maxDisplacement < threshold) {
      stableFrameCount++;
    } else {
      stableFrameCount = 1;
    }

    return stableFrameCount >= STABILITY_FRAMES;
  }

  /**
   * Check if the detected card region is in focus using Laplacian variance.
   *
   * @param {Array<{x: number, y: number}>} corners - Card boundary corners
   * @returns {boolean} True if Laplacian variance ≥ FOCUS_THRESHOLD
   */
  function focusCheck(corners) {
    if (!cvReady || !offscreenCanvas) {
      return false;
    }

    // Read the source image from the offscreen canvas
    var src = cv.imread(offscreenCanvas);
    var gray = new cv.Mat();
    var laplacian = new cv.Mat();
    var mean = new cv.Mat();
    var stddev = new cv.Mat();

    try {
      // Create a mask for the ROI defined by the corners
      var mask = cv.Mat.zeros(src.rows, src.cols, cv.CV_8UC1);
      var roiPoints = cv.matFromArray(4, 1, cv.CV_32SC2, [
        corners[0].x, corners[0].y,
        corners[1].x, corners[1].y,
        corners[2].x, corners[2].y,
        corners[3].x, corners[3].y
      ]);
      var pts = new cv.MatVector();
      pts.push_back(roiPoints);
      // Use drawContours with FILLED since fillPoly is not available in opencv.js
      cv.drawContours(mask, pts, 0, new cv.Scalar(255), cv.FILLED);

      // Convert source to grayscale
      cv.cvtColor(src, gray, cv.COLOR_RGBA2GRAY);

      // Compute Laplacian
      cv.Laplacian(gray, laplacian, cv.CV_64F);

      // Compute mean and stddev on the ROI (masked area)
      cv.meanStdDev(laplacian, mean, stddev, mask);

      // Variance = stddev^2
      var std = stddev.doubleAt(0, 0);
      var variance = std * std;

      mask.delete();
      roiPoints.delete();
      pts.delete();

      return variance >= FOCUS_THRESHOLD;
    } finally {
      src.delete();
      gray.delete();
      laplacian.delete();
      mean.delete();
      stddev.delete();
    }
  }

  /**
   * Check if the detected card region meets minimum size requirements.
   *
   * @param {Array<{x: number, y: number}>} corners - Detected card corners
   * @returns {boolean} True if the card region is large enough
   */
  function sizeCheck(corners) {
    var ordered = orderCorners(corners);
    var tl = ordered[0], tr = ordered[1], br = ordered[2], bl = ordered[3];

    var widthTop = distance(tl, tr);
    var widthBottom = distance(bl, br);
    var computedWidth = Math.max(widthTop, widthBottom);

    var heightLeft = distance(tl, bl);
    var heightRight = distance(tr, br);
    var computedHeight = Math.max(heightLeft, heightRight);

    return computedWidth >= OUTPUT_W && computedHeight >= OUTPUT_H;
  }

  /** Circled number characters for countdown display (index 0 = ❺, index 4 = ❶) */
  var COUNTDOWN_CHARS = ['\u277A', '\u2779', '\u2778', '\u2777', '\u2776']; // ❺ ❹ ❸ ❷ ❶

  /**
   * Render the detected boundary polygon on the overlay canvas.
   *
   * @param {Array<{x: number, y: number}>|null} corners - Card boundary
   *   corners, or null to clear the overlay.
   * @param {boolean} [tooSmall=false] - If true, render in red with a
   *   magnifying glass icon indicating user should get closer.
   * @param {number} [countdown=0] - Stable frame count (1..STABILITY_FRAMES).
   *   Displayed as a countdown from ❺ to ❶, capturing after ❶.
   */
  function renderOverlay(corners, tooSmall, countdown) {
    if (!overlayEl) {
      return;
    }

    var ctx = overlayEl.getContext('2d');
    var displayWidth = overlayEl.clientWidth;
    var displayHeight = overlayEl.clientHeight;

    // Ensure canvas resolution matches display size
    if (overlayEl.width !== displayWidth || overlayEl.height !== displayHeight) {
      overlayEl.width = displayWidth;
      overlayEl.height = displayHeight;
    }

    // Clear previous frame
    ctx.clearRect(0, 0, overlayEl.width, overlayEl.height);

    if (!corners || corners.length !== 4) {
      return;
    }

    // Scale corners from video coordinates to overlay coordinates
    var vw = videoEl.videoWidth;
    var vh = videoEl.videoHeight;
    if (vw === 0 || vh === 0) {
      return;
    }

    var scaleX = overlayEl.width / vw;
    var scaleY = overlayEl.height / vh;

    var scaledCorners = [];
    for (var i = 0; i < 4; i++) {
      var sx = corners[i].x * scaleX;
      var sy = corners[i].y * scaleY;
      scaledCorners.push({ x: sx, y: sy });
    }

    // Choose color based on size
    var strokeColor = tooSmall ? '#ff3333' : '#00ff00';
    var fillColor = tooSmall ? 'rgba(255, 50, 50, 0.1)' : 'rgba(0, 255, 0, 0.1)';
    var dotColor = tooSmall ? '#ff3333' : '#00ff00';

    // Draw the polygon
    ctx.beginPath();
    ctx.moveTo(scaledCorners[0].x, scaledCorners[0].y);
    for (var j = 1; j < 4; j++) {
      ctx.lineTo(scaledCorners[j].x, scaledCorners[j].y);
    }
    ctx.closePath();

    ctx.strokeStyle = strokeColor;
    ctx.lineWidth = 3;
    ctx.fillStyle = fillColor;
    ctx.fill();
    ctx.stroke();

    // Draw corner dots
    ctx.fillStyle = dotColor;
    for (var k = 0; k < 4; k++) {
      ctx.beginPath();
      ctx.arc(scaledCorners[k].x, scaledCorners[k].y, 6, 0, 2 * Math.PI);
      ctx.fill();
    }

    // Center point of the bounding box
    var cx = (scaledCorners[0].x + scaledCorners[1].x + scaledCorners[2].x + scaledCorners[3].x) / 4;
    var cy = (scaledCorners[0].y + scaledCorners[1].y + scaledCorners[2].y + scaledCorners[3].y) / 4;

    if (tooSmall) {
      // Draw a magnifying glass icon in the center
      var iconSize = 24;

      ctx.beginPath();
      ctx.arc(cx - 4, cy - 4, iconSize, 0, 2 * Math.PI);
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.9)';
      ctx.lineWidth = 4;
      ctx.stroke();

      ctx.beginPath();
      ctx.moveTo(cx - 4 + iconSize * 0.7, cy - 4 + iconSize * 0.7);
      ctx.lineTo(cx - 4 + iconSize * 1.3, cy - 4 + iconSize * 1.3);
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.9)';
      ctx.lineWidth = 5;
      ctx.lineCap = 'round';
      ctx.stroke();

      ctx.font = 'bold ' + (iconSize * 1.2) + 'px sans-serif';
      ctx.fillStyle = 'rgba(255, 255, 255, 0.9)';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('+', cx - 4, cy - 4);
    } else if (countdown > 0 && countdown <= COUNTDOWN_CHARS.length) {
      // Show countdown: stableFrameCount 1=❺, 2=❹, 3=❸, 4=❷, 5=❶
      var charIndex = countdown - 1;
      var countdownChar = COUNTDOWN_CHARS[charIndex];
      ctx.font = 'bold 192px sans-serif';
      ctx.fillStyle = 'rgba(255, 255, 255, 0.9)';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(countdownChar, cx, cy);
    }
  }

  /**
   * Main detection loop — called via requestAnimationFrame.
   * Processes one frame per tick: capture → size check → stability → focus → auto-capture.
   */
  var _loopLogCounter = 0;
  function detectionLoop() {
    if (!detectionRunning) {
      return;
    }

    var result = captureFrame();
    _loopLogCounter++;

    if (result && result.corners) {
      // Check if the card region is large enough
      var largeEnough = sizeCheck(result.corners);

      if (!largeEnough) {
        // Card detected but too small — show red overlay with magnifying glass
        if (_loopLogCounter % 30 === 0) {
          debugLog('[Scanner] Card detected but too small — get closer');
        }
        renderOverlay(result.corners, true, 0);
        previousCorners = null;
        stableFrameCount = 0;
      } else {
        // Check stability
        var stable = stabilityCheck(result.corners);

        // Card boundary detected and large enough — render green overlay with countdown
        renderOverlay(result.corners, false, stableFrameCount);

        if (stable) {
          // Check focus
          var focused = focusCheck(result.corners);

          if (focused) {
            debugLog('[Scanner] Auto-capture triggered — stable & focused');
            // Card is stable, large enough, and in focus — trigger auto-capture
            detectionRunning = false;
            triggerAutoCapture(result.corners);
            return;
          } else {
            if (_loopLogCounter % 30 === 0) {
              debugLog('[Scanner] Stable but focus check failed — hold steady');
            }
          }
        }
      }
    } else {
      // No card detected — clear overlay, reset stability
      if (_loopLogCounter % 60 === 0) {
        debugLog('[Scanner] No card boundary detected');
      }
      renderOverlay(null);
      previousCorners = null;
      stableFrameCount = 0;
    }

    rafId = requestAnimationFrame(detectionLoop);
  }

  /**
   * Start the detection pipeline loop.
   */
  function startDetection() {
    if (!cvReady) {
      debugLog('[Scanner] OpenCV not ready — detection deferred');
      return;
    }
    debugLog('[Scanner] Detection loop started (video: ' +
      videoEl.videoWidth + 'x' + videoEl.videoHeight + ')');
    detectionRunning = true;
    previousCorners = null;
    stableFrameCount = 0;
    _loopLogCounter = 0;
    rafId = requestAnimationFrame(detectionLoop);
  }

  /**
   * Stop the detection pipeline loop.
   */
  function stopDetection() {
    detectionRunning = false;
    if (rafId !== null) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
    renderOverlay(null);
  }

  // =========================================================================
  // Image Rotation
  // =========================================================================

  /** Current rotation applied to the captured image (degrees, multiple of 90) */
  var captureRotation = 0;

  /**
   * Rotate a JPEG data URL by the given degrees (90, 180, 270).
   *
   * @param {string} dataUrl - Source image data URL
   * @param {number} degrees - Rotation in degrees (must be multiple of 90)
   * @returns {Promise<string>} Rotated image data URL
   */
  function rotateDataUrl(dataUrl, degrees) {
    return new Promise(function(resolve) {
      var img = new Image();
      img.onload = function() {
        var canvas = document.createElement('canvas');
        var ctx = canvas.getContext('2d');
        var rad = (degrees * Math.PI) / 180;

        if (degrees === 90 || degrees === 270) {
          canvas.width = img.height;
          canvas.height = img.width;
        } else {
          canvas.width = img.width;
          canvas.height = img.height;
        }

        ctx.translate(canvas.width / 2, canvas.height / 2);
        ctx.rotate(rad);
        ctx.drawImage(img, -img.width / 2, -img.height / 2);
        resolve(canvas.toDataURL('image/jpeg', 0.95));
      };
      img.src = dataUrl;
    });
  }

  /**
   * Apply rotation to the current captured image and update the preview.
   *
   * @param {number} addDegrees - Degrees to add (90 for CW, -90 for CCW)
   */
  async function applyRotation(addDegrees) {
    if (!capturedDataUrl) return;

    captureRotation = (captureRotation + addDegrees + 360) % 360;

    if (captureRotation === 0) {
      // Back to original
      capturePreviewEl.src = capturedDataUrl;
    } else {
      var rotated = await rotateDataUrl(capturedDataUrl, captureRotation);
      capturePreviewEl.src = rotated;
    }
    // Reset zoom state
    capturePreviewEl.classList.remove('zoomed');
  }

  /**
   * Get the final (rotated) data URL for submission.
   *
   * @returns {Promise<string>} The data URL with rotation applied
   */
  async function getFinalDataUrl() {
    if (captureRotation === 0) {
      return capturedDataUrl;
    }
    return rotateDataUrl(capturedDataUrl, captureRotation);
  }

  // =========================================================================
  // Perspective Transform and Auto-Capture
  // =========================================================================

  /**
   * Order four corner points as: Top-Left, Top-Right, Bottom-Right, Bottom-Left.
   * Uses sum (x+y) to find TL (min sum) and BR (max sum),
   * and difference (y-x) to find TR (min diff) and BL (max diff).
   *
   * @param {Array<{x: number, y: number}>} corners - Four detected corners
   * @returns {Array<{x: number, y: number}>} Corners ordered [TL, TR, BR, BL]
   */
  function orderCorners(corners) {
    var sorted = corners.slice();

    // Compute sums and differences
    var sums = sorted.map(function (p) { return p.x + p.y; });
    var diffs = sorted.map(function (p) { return p.y - p.x; });

    // TL has smallest sum, BR has largest sum
    var tlIdx = sums.indexOf(Math.min.apply(null, sums));
    var brIdx = sums.indexOf(Math.max.apply(null, sums));

    // TR has smallest difference, BL has largest difference
    var trIdx = diffs.indexOf(Math.min.apply(null, diffs));
    var blIdx = diffs.indexOf(Math.max.apply(null, diffs));

    return [sorted[tlIdx], sorted[trIdx], sorted[brIdx], sorted[blIdx]];
  }

  /**
   * Compute Euclidean distance between two points.
   *
   * @param {{x: number, y: number}} a
   * @param {{x: number, y: number}} b
   * @returns {number}
   */
  function distance(a, b) {
    var dx = a.x - b.x;
    var dy = a.y - b.y;
    return Math.sqrt(dx * dx + dy * dy);
  }

  /**
   * Apply perspective transform to extract and rectify the card region.
   * Orders corners (TL, TR, BR, BL), computes output dimensions enforcing
   * minimum OUTPUT_W × OUTPUT_H, applies getPerspectiveTransform + warpPerspective,
   * and encodes the result as a JPEG data URL.
   *
   * @param {Array<{x: number, y: number}>} corners - Four detected card corners
   * @returns {string|null} JPEG data URL of the rectified card, or null on failure
   */
  function perspectiveTransform(corners) {
    if (!cvReady || !offscreenCanvas) {
      return null;
    }

    var ordered = orderCorners(corners);
    var tl = ordered[0];
    var tr = ordered[1];
    var br = ordered[2];
    var bl = ordered[3];

    // Compute widths and heights from the source quadrilateral
    var widthTop = distance(tl, tr);
    var widthBottom = distance(bl, br);
    var computedWidth = Math.max(widthTop, widthBottom);

    var heightLeft = distance(tl, bl);
    var heightRight = distance(tr, br);
    var computedHeight = Math.max(heightLeft, heightRight);

    // Enforce minimum output dimensions — reject if too small (never upscale)
    var outW = Math.round(computedWidth);
    var outH = Math.round(computedHeight);
    if (outW < OUTPUT_W || outH < OUTPUT_H) {
      // Card region is too small — caller should show "get closer" feedback
      return null;
    }
    // Read the source frame from offscreen canvas
    var src = cv.imread(offscreenCanvas);
    var dst = new cv.Mat();
    var M = null;
    var srcPoints = null;
    var dstPoints = null;
    var dsize = null;

    try {
      // Source points (ordered corners)
      srcPoints = cv.matFromArray(4, 1, cv.CV_32FC2, [
        tl.x, tl.y,
        tr.x, tr.y,
        br.x, br.y,
        bl.x, bl.y
      ]);

      // Destination points (rectangle)
      dstPoints = cv.matFromArray(4, 1, cv.CV_32FC2, [
        0, 0,
        outW - 1, 0,
        outW - 1, outH - 1,
        0, outH - 1
      ]);

      // Compute the perspective transform matrix
      M = cv.getPerspectiveTransform(srcPoints, dstPoints);

      // Apply warp
      dsize = new cv.Size(outW, outH);
      cv.warpPerspective(src, dst, M, dsize);

      // Encode the result to a canvas, then extract JPEG data URL
      var outputCanvas = document.createElement('canvas');
      outputCanvas.width = outW;
      outputCanvas.height = outH;
      cv.imshow(outputCanvas, dst);

      var dataUrl = outputCanvas.toDataURL('image/jpeg', 0.95);
      return dataUrl;
    } catch (e) {
      console.error('perspectiveTransform failed:', e);
      return null;
    } finally {
      src.delete();
      dst.delete();
      if (M) { M.delete(); }
      if (srcPoints) { srcPoints.delete(); }
      if (dstPoints) { dstPoints.delete(); }
    }
  }

  /**
   * Play a shutter sound to confirm auto-capture.
   * Uses the Web Audio API to generate a short click/shutter tone.
   */
  function playShutterSound() {
    try {
      var audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      var oscillator = audioCtx.createOscillator();
      var gainNode = audioCtx.createGain();

      oscillator.connect(gainNode);
      gainNode.connect(audioCtx.destination);

      oscillator.type = 'square';
      oscillator.frequency.setValueAtTime(800, audioCtx.currentTime);
      oscillator.frequency.setValueAtTime(600, audioCtx.currentTime + 0.05);

      gainNode.gain.setValueAtTime(0.3, audioCtx.currentTime);
      gainNode.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.15);

      oscillator.start(audioCtx.currentTime);
      oscillator.stop(audioCtx.currentTime + 0.15);
    } catch (e) {
      // Audio not available — silent capture is acceptable
    }
  }

  /**
   * Trigger auto-capture: apply perspective transform, auto-rotate if landscape,
   * play shutter sound, and transition to the confirmation screen.
   *
   * @param {Array<{x: number, y: number}>} corners - Detected card corners
   */
  function triggerAutoCapture(corners) {
    var dataUrl = perspectiveTransform(corners);

    if (!dataUrl) {
      // Transform failed — resume detection
      startDetection();
      return;
    }

    // Play shutter sound
    playShutterSound();

    // Store the captured data URL (original, unrotated)
    capturedDataUrl = dataUrl;
    captureRotation = 0;

    // Check if image is landscape (width > height) and auto-rotate
    var img = new Image();
    img.onload = function() {
      if (img.width > img.height) {
        // Landscape — auto-rotate 90° CW
        captureRotation = 90;
        rotateDataUrl(dataUrl, 90).then(function(rotated) {
          transitionToConfirmation(rotated);
        });
      } else {
        transitionToConfirmation(dataUrl);
      }
    };
    img.src = dataUrl;
  }

  // =========================================================================
  // Toast Management
  // =========================================================================

  /**
   * Hide all toast elements.
   */
  function hideAllToasts() {
    if (toastSuccessEl) {
      toastSuccessEl.classList.remove('active');
    }
    if (toastErrorEl) {
      toastErrorEl.classList.remove('active');
    }
  }

  /**
   * Show a success toast with the given message.
   *
   * @param {string} message
   */
  function showSuccessToast(message) {
    hideAllToasts();
    if (toastSuccessEl) {
      toastSuccessEl.textContent = message;
      toastSuccessEl.classList.add('active');
    }
  }

  /**
   * Show an error toast with the given message.
   *
   * @param {string} message
   */
  function showErrorToast(message) {
    hideAllToasts();
    if (toastErrorEl) {
      toastErrorEl.textContent = message;
      toastErrorEl.classList.add('active');
    }
  }

  // =========================================================================
  // Spinner and Control State
  // =========================================================================

  /**
   * Show the spinner overlay and disable Accept/Reject buttons.
   */
  function showSpinnerAndDisableControls() {
    if (spinnerOverlayEl) {
      spinnerOverlayEl.classList.add('active');
    }
    if (acceptBtn) {
      acceptBtn.disabled = true;
    }
    if (rejectBtn) {
      rejectBtn.disabled = true;
    }
  }

  /**
   * Hide the spinner overlay and re-enable Accept/Reject buttons.
   */
  function hideSpinnerAndEnableControls() {
    if (spinnerOverlayEl) {
      spinnerOverlayEl.classList.remove('active');
    }
    if (acceptBtn) {
      acceptBtn.disabled = false;
    }
    if (rejectBtn) {
      rejectBtn.disabled = false;
    }
  }

  // =========================================================================
  // Swipe Gesture Handling
  // =========================================================================

  /**
   * Handle touchstart event on the capture preview for swipe-up gesture.
   *
   * @param {TouchEvent} e
   */
  function onSwipeTouchStart(e) {
    if (e.touches.length === 1) {
      swipeTouchStartY = e.touches[0].clientY;
    }
  }

  /**
   * Handle touchend event on the capture preview for swipe-up gesture.
   * If vertical upward delta > 80 px, treat as accept.
   *
   * @param {TouchEvent} e
   */
  function onSwipeTouchEnd(e) {
    if (swipeTouchStartY === null) {
      return;
    }

    var touchEndY = e.changedTouches[0].clientY;
    var deltaY = swipeTouchStartY - touchEndY; // positive = upward swipe

    swipeTouchStartY = null;

    if (deltaY > 80) {
      // Swipe up detected — accept the card
      if (capturedDataUrl) {
        getFinalDataUrl().then(function(finalUrl) {
          submitCard(finalUrl);
        });
      }
    }
  }

  /**
   * Attach swipe-up gesture listeners to the capture preview element.
   */
  function addSwipeListeners() {
    if (capturePreviewEl) {
      capturePreviewEl.addEventListener('touchstart', onSwipeTouchStart);
      capturePreviewEl.addEventListener('touchend', onSwipeTouchEnd);
    }
  }

  /**
   * Remove swipe-up gesture listeners from the capture preview element.
   */
  function removeSwipeListeners() {
    if (capturePreviewEl) {
      capturePreviewEl.removeEventListener('touchstart', onSwipeTouchStart);
      capturePreviewEl.removeEventListener('touchend', onSwipeTouchEnd);
    }
    swipeTouchStartY = null;
  }

  // =========================================================================
  // Card Submission
  // =========================================================================

  /**
   * Convert a data URL to a Blob.
   *
   * @param {string} dataUrl - JPEG data URL
   * @returns {Blob}
   */
  function dataUrlToBlob(dataUrl) {
    var parts = dataUrl.split(',');
    var mimeMatch = parts[0].match(/:(.*?);/);
    var mime = mimeMatch ? mimeMatch[1] : 'image/jpeg';
    var byteString = atob(parts[1]);
    var byteArray = new Uint8Array(byteString.length);
    for (var i = 0; i < byteString.length; i++) {
      byteArray[i] = byteString.charCodeAt(i);
    }
    return new Blob([byteArray], { type: mime });
  }

  /**
   * Submit the captured card image to the server.
   *
   * Converts the JPEG data URL to a Blob, builds a FormData with field
   * `card_image`, POSTs to `/scan` with a 30-second timeout.
   *
   * On 201: shows success toast with record ID for ≥ 2 s, returns to State 1.
   * On 4xx/5xx: shows server error toast, re-enables controls.
   * On network error or timeout: shows connectivity error toast, re-enables controls.
   *
   * @param {string} jpegDataUrl - The JPEG data URL to submit
   */
  async function submitCard(jpegDataUrl) {
    // Show spinner and disable controls
    showSpinnerAndDisableControls();
    hideAllToasts();

    // Convert data URL to Blob and build FormData
    var blob = dataUrlToBlob(jpegDataUrl);
    var formData = new FormData();
    formData.append('card_image', blob, 'card.jpg');

    // Set up AbortController with 30-second timeout
    var controller = new AbortController();
    var timeoutId = setTimeout(function () {
      controller.abort();
    }, 30000);

    try {
      var response = await fetch('/api/scan', {
        method: 'POST',
        body: formData,
        signal: controller.signal
      });

      clearTimeout(timeoutId);

      if (response.status === 201) {
        // Success — parse response and show record ID
        var data = await response.json();
        var recordId = data.record_id || 'unknown';
        hideSpinnerAndEnableControls();
        showSuccessToast('Card saved — Record #' + recordId);

        // Wait at least 2 seconds, then return to live preview
        setTimeout(function () {
          hideAllToasts();
          transitionToLivePreview();
        }, 2000);
      } else {
        // Server error (4xx/5xx)
        var errorData;
        try {
          errorData = await response.json();
        } catch (e) {
          errorData = null;
        }
        var errorMessage = (errorData && errorData.detail)
          ? errorData.detail
          : 'Server error (' + response.status + ')';
        hideSpinnerAndEnableControls();
        showErrorToast(errorMessage);
      }
    } catch (err) {
      clearTimeout(timeoutId);
      hideSpinnerAndEnableControls();

      if (err.name === 'AbortError') {
        showErrorToast('Request timed out — please check your connection and try again.');
      } else {
        showErrorToast('Network error — please check your connection and try again.');
      }
    }
  }

  // =========================================================================
  // Confirmation Screen State Transitions
  // =========================================================================

  /**
   * Transition from live preview (State 1) to confirmation screen (State 2).
   *
   * @param {string} dataUrl - JPEG data URL of the captured card
   */
  function transitionToConfirmation(dataUrl) {
    // Stop detection and clear overlay
    stopDetection();
    renderOverlay(null);

    // Hide all toasts from any previous interaction
    hideAllToasts();

    // Hide live preview, show confirmation
    if (livePreviewStateEl) {
      livePreviewStateEl.style.display = 'none';
    }
    if (confirmationStateEl) {
      confirmationStateEl.style.display = 'block';
    }
    if (capturePreviewEl) {
      capturePreviewEl.src = dataUrl;
      capturePreviewEl.classList.remove('zoomed');
    }

    // Add swipe-up gesture listener
    addSwipeListeners();
  }

  /**
   * Toggle zoom on the capture preview image.
   * Tap once to zoom to 100% (native pixels), tap again to fit-to-width.
   */
  function togglePreviewZoom(e) {
    if (!capturePreviewEl) return;

    if (capturePreviewEl.classList.contains('zoomed')) {
      capturePreviewEl.classList.remove('zoomed');
    } else {
      capturePreviewEl.classList.add('zoomed');

      // Scroll to the tapped point after zooming
      var wrapper = document.getElementById('previewWrapper');
      if (wrapper) {
        var rect = wrapper.getBoundingClientRect();
        var tapX = (e.clientX || (e.changedTouches && e.changedTouches[0].clientX) || rect.width / 2) - rect.left;
        var tapY = (e.clientY || (e.changedTouches && e.changedTouches[0].clientY) || rect.height / 2) - rect.top;
        // Scale tap position to full image coordinates
        var scaleX = capturePreviewEl.naturalWidth / rect.width;
        var scaleY = capturePreviewEl.naturalHeight / rect.height;
        // After class change, scroll to center the tapped point
        setTimeout(function() {
          wrapper.scrollLeft = (tapX * scaleX) - (wrapper.clientWidth / 2);
          wrapper.scrollTop = (tapY * scaleY) - (wrapper.clientHeight / 2);
        }, 0);
      }
    }
  }

  /**
   * Transition from confirmation screen (State 2) back to live preview (State 1).
   */
  function transitionToLivePreview() {
    // Remove swipe gesture listeners
    removeSwipeListeners();

    // Clear captured data
    capturedDataUrl = null;

    // Hide all toasts
    hideAllToasts();

    // Hide confirmation, show live preview
    if (confirmationStateEl) {
      confirmationStateEl.style.display = 'none';
    }
    if (livePreviewStateEl) {
      livePreviewStateEl.style.display = 'block';
    }
    if (capturePreviewEl) {
      capturePreviewEl.src = '';
      capturePreviewEl.classList.remove('zoomed');
    }

    // Reset stability and restart detection
    previousCorners = null;
    stableFrameCount = 0;
    startDetection();
  }

  // =========================================================================
  // Initialization
  // =========================================================================

  /**
   * Wait for OpenCV.js to be ready, then start the detection loop.
   */
  function initOpenCV() {
    if (typeof cv !== 'undefined' && cv.Mat) {
      cvReady = true;
      startDetection();
    } else if (typeof cv !== 'undefined' && cv.onRuntimeInitialized !== undefined) {
      // OpenCV.js loaded but WASM not yet initialized
      var originalCallback = cv.onRuntimeInitialized;
      cv.onRuntimeInitialized = function () {
        if (originalCallback) {
          originalCallback();
        }
        cvReady = true;
        startDetection();
      };
    } else {
      // OpenCV.js not yet loaded — poll
      var pollCount = 0;
      var pollInterval = setInterval(function () {
        pollCount++;
        if (typeof cv !== 'undefined' && cv.Mat) {
          clearInterval(pollInterval);
          cvReady = true;
          startDetection();
        } else if (pollCount > 100) {
          // Give up after ~10 seconds
          clearInterval(pollInterval);
          console.warn('OpenCV.js failed to initialize');
        }
      }, 100);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    // Grab DOM references — live preview
    videoEl = document.getElementById('preview');
    overlayEl = document.getElementById('overlay');
    switchBtn = document.getElementById('switchCamera');
    errorPermissionEl = document.getElementById('errorPermission');
    errorUnsupportedEl = document.getElementById('errorUnsupported');
    debugOverlayEl = document.getElementById('debugOverlay');

    // Grab DOM references — confirmation screen
    livePreviewStateEl = document.getElementById('livePreviewState');
    confirmationStateEl = document.getElementById('confirmationState');
    capturePreviewEl = document.getElementById('capturePreview');
    acceptBtn = document.getElementById('acceptBtn');
    rejectBtn = document.getElementById('rejectBtn');
    spinnerOverlayEl = document.getElementById('spinnerOverlay');
    toastSuccessEl = document.getElementById('toastSuccess');
    toastErrorEl = document.getElementById('toastError');

    // Check for getUserMedia support
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showUnsupportedError();
      return;
    }

    // iOS Safari blocks getUserMedia on non-HTTPS origins (except localhost).
    // Detect if we're on an insecure context and show a helpful message.
    var isSecureContext = window.isSecureContext ||
      location.protocol === 'https:' ||
      location.hostname === 'localhost' ||
      location.hostname === '127.0.0.1';

    if (!isSecureContext) {
      // Show a warning but still attempt — some browsers are more lenient
      console.warn('Camera access may require HTTPS on this device.');
    }

    // Start camera — on iOS this may require user gesture, so we also
    // wire it to a tap on the video element as a fallback
    startCamera(null);

    // Fallback: if the video doesn't start playing within 2s,
    // show a "tap to start" prompt (helps with iOS gesture requirement)
    var cameraStartTimeout = setTimeout(function () {
      if (!currentStream || (videoEl && videoEl.readyState < 2)) {
        videoEl.style.cursor = 'pointer';
        videoEl.setAttribute('aria-label', 'Tap to start camera');
        var tapHandler = function () {
          videoEl.removeEventListener('click', tapHandler);
          videoEl.removeEventListener('touchend', tapHandler);
          videoEl.style.cursor = '';
          startCamera(null);
        };
        videoEl.addEventListener('click', tapHandler);
        videoEl.addEventListener('touchend', tapHandler);
      }
    }, 2000);

    // Wire up switch camera button
    if (switchBtn) {
      switchBtn.addEventListener('click', function () {
        switchCamera();
      });
    }

    // Wire up reject button — return to live preview
    if (rejectBtn) {
      rejectBtn.addEventListener('click', function () {
        transitionToLivePreview();
      });
    }

    // Wire up accept button — submit card
    if (acceptBtn) {
      acceptBtn.addEventListener('click', function () {
        if (capturedDataUrl && typeof submitCard === 'function') {
          getFinalDataUrl().then(function(finalUrl) {
            submitCard(finalUrl);
          });
        }
      });
    }

    // Wire up tap-to-zoom on capture preview
    if (capturePreviewEl) {
      capturePreviewEl.addEventListener('click', togglePreviewZoom);
    }

    // Wire up rotation buttons
    var rotateCCW = document.getElementById('rotateCCW');
    var rotateCW = document.getElementById('rotateCW');
    if (rotateCCW) {
      rotateCCW.addEventListener('click', function(e) {
        e.stopPropagation();
        applyRotation(-90);
      });
    }
    if (rotateCW) {
      rotateCW.addEventListener('click', function(e) {
        e.stopPropagation();
        applyRotation(90);
      });
    }

    // Initialize OpenCV.js detection pipeline
    initOpenCV();
  });

})();
