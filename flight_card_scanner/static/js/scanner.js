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

  /** Max corner displacement (px) between frames to count as stable */
  var STABILITY_THRESHOLD = 10;

  /** Consecutive stable frames required before auto-capture */
  var STABILITY_FRAMES = 8;

  /** Laplacian variance threshold for focus check */
  var FOCUS_THRESHOLD = 80.0;

  /** Minimum output width (px) after perspective correction */
  var OUTPUT_W = 1000;

  /** Minimum output height (px) after perspective correction */
  var OUTPUT_H = 1300;

  // =========================================================================
  // Module-level State
  // =========================================================================

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
          deviceId: { exact: deviceId }
        },
        audio: false
      };
    } else {
      // First call — prefer environment-facing (rear) camera
      // Keep constraints minimal for maximum iOS compatibility
      constraints = {
        video: {
          facingMode: { ideal: 'environment' }
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
      }
    } catch (err) {
      // On iOS, if facingMode constraint fails, retry with basic video: true
      if (!deviceId && err.name === 'OverconstrainedError') {
        try {
          currentStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
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

  // =========================================================================
  // OpenCV.js Detection Pipeline
  // =========================================================================

  /**
   * Capture and process a single video frame through the OpenCV.js pipeline.
   * Steps: draw video → grayscale → blur → Canny → findContours →
   * approxPolyDP → select largest 4-vertex contour → area check.
   *
   * @returns {{corners: Array<{x: number, y: number}>}|null} Detected card
   *   corners or null if no valid card found.
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

    // Ensure offscreen canvas matches video dimensions
    if (!offscreenCanvas) {
      offscreenCanvas = document.createElement('canvas');
      offscreenCtx = offscreenCanvas.getContext('2d');
    }
    if (offscreenCanvas.width !== vw || offscreenCanvas.height !== vh) {
      offscreenCanvas.width = vw;
      offscreenCanvas.height = vh;
    }

    // Draw video frame to offscreen canvas
    offscreenCtx.drawImage(videoEl, 0, 0, vw, vh);

    // OpenCV Mat from canvas
    var src = cv.imread(offscreenCanvas);
    var gray = new cv.Mat();
    var blurred = new cv.Mat();
    var edges = new cv.Mat();
    var contours = new cv.MatVector();
    var hierarchy = new cv.Mat();

    try {
      // Convert to grayscale
      cv.cvtColor(src, gray, cv.COLOR_RGBA2GRAY);

      // Gaussian blur (5×5, σ=0)
      var ksize = new cv.Size(5, 5);
      cv.GaussianBlur(gray, blurred, ksize, 0);

      // Canny edge detection (threshold1=75, threshold2=200)
      cv.Canny(blurred, edges, 75, 200);

      // Find external contours
      cv.findContours(edges, contours, hierarchy, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE);

      var frameArea = vw * vh;
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
        // Extract corner points from the best contour
        var corners = [];
        for (var j = 0; j < 4; j++) {
          corners.push({
            x: bestContour.data32S[j * 2],
            y: bestContour.data32S[j * 2 + 1]
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
   * Requires < STABILITY_THRESHOLD px displacement for STABILITY_FRAMES
   * consecutive frames.
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

    if (maxDisplacement < STABILITY_THRESHOLD) {
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
   * Render the detected boundary polygon on the overlay canvas.
   *
   * @param {Array<{x: number, y: number}>|null} corners - Card boundary
   *   corners, or null to clear the overlay.
   */
  function renderOverlay(corners) {
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

    // Draw the polygon
    ctx.beginPath();
    ctx.moveTo(corners[0].x * scaleX, corners[0].y * scaleY);
    for (var i = 1; i < 4; i++) {
      ctx.lineTo(corners[i].x * scaleX, corners[i].y * scaleY);
    }
    ctx.closePath();

    // Style: green semi-transparent fill with solid border
    ctx.strokeStyle = '#00ff00';
    ctx.lineWidth = 3;
    ctx.fillStyle = 'rgba(0, 255, 0, 0.1)';
    ctx.fill();
    ctx.stroke();

    // Draw corner dots
    ctx.fillStyle = '#00ff00';
    for (var j = 0; j < 4; j++) {
      ctx.beginPath();
      ctx.arc(corners[j].x * scaleX, corners[j].y * scaleY, 6, 0, 2 * Math.PI);
      ctx.fill();
    }
  }

  /**
   * Main detection loop — called via requestAnimationFrame.
   * Processes one frame per tick: capture → stability → focus → auto-capture.
   */
  function detectionLoop() {
    if (!detectionRunning) {
      return;
    }

    var result = captureFrame();

    if (result && result.corners) {
      // Card boundary detected — render overlay
      renderOverlay(result.corners);

      // Check stability
      var stable = stabilityCheck(result.corners);

      if (stable) {
        // Check focus
        var focused = focusCheck(result.corners);

        if (focused) {
          // Card is stable and in focus — trigger auto-capture
          detectionRunning = false;
          triggerAutoCapture(result.corners);
          return;
        }
      }
    } else {
      // No card detected — clear overlay, reset stability
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
      return;
    }
    detectionRunning = true;
    previousCorners = null;
    stableFrameCount = 0;
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

    // Enforce minimum output dimensions
    var outW = Math.max(OUTPUT_W, Math.round(computedWidth));
    var outH = Math.max(OUTPUT_H, Math.round(computedHeight));

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

      var dataUrl = outputCanvas.toDataURL('image/jpeg', 0.92);
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
   * Trigger auto-capture: apply perspective transform, play shutter sound,
   * and transition to the confirmation screen.
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

    // Store the captured data URL
    capturedDataUrl = dataUrl;

    // Transition to confirmation screen (State 2)
    transitionToConfirmation(dataUrl);
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
        submitCard(capturedDataUrl);
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
    }

    // Add swipe-up gesture listener
    addSwipeListeners();
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
          submitCard(capturedDataUrl);
        }
      });
    }

    // Initialize OpenCV.js detection pipeline
    initOpenCV();
  });

})();
