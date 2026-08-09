(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const debounce = (fn, delay = 400) => { let timer; return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); }; };
  const throttle = (fn, delay = 1000) => { let last = 0; return (...args) => { const now = Date.now(); if (now - last >= delay) { last = now; fn(...args); } }; };
  const randomId = () => globalThis.crypto?.randomUUID?.() || `smartreco-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const formatInr = (value) => {
    const amount = Number(value);
    if (!Number.isFinite(amount)) return "0";
    const hasPaise = Math.abs(amount - Math.round(amount)) > 0.000001;
    return amount.toLocaleString("en-IN", {
      minimumFractionDigits: hasPaise ? 2 : 0,
      maximumFractionDigits: hasPaise ? 2 : 0,
    });
  };
  const toast = (message, type = "success") => {
    const node = document.createElement("div");
    node.className = `toast ${type}`;
    node.textContent = message;
    $("#toast-stack")?.append(node);
    setTimeout(() => node.remove(), 3600);
  };
  try {
    const purchaseNotice = sessionStorage.getItem("smartreco_purchase_notice");
    if (purchaseNotice) {
      sessionStorage.removeItem("smartreco_purchase_notice");
      toast(purchaseNotice);
    }
  } catch { /* Purchase completion does not depend on transient UI storage. */ }

  class SmartRecoTracker {
    constructor() {
      this.key = "smartreco_event_queue_v1";
      this.sessionKey = "smartreco_session_id";
      this.queue = this.readQueue();
      try { this.sessionId = sessionStorage.getItem(this.sessionKey) || randomId(); }
      catch { this.sessionId = randomId(); }
      try { sessionStorage.setItem(this.sessionKey, this.sessionId); } catch { /* UI remains functional without storage. */ }
      try { this.enabled = localStorage.getItem("smartreco_consent") === "granted"; }
      catch { this.enabled = false; }
      this.recentSignals = {};
      try { this.recentSignals = JSON.parse(sessionStorage.getItem("smartreco_recent_signals") || "{}"); }
      catch { this.recentSignals = {}; }
      this.flushing = false;
      this.flushTimer = setInterval(() => this.flush(), 10000);
      this.bindLifecycle();
    }
    readQueue() { try { return JSON.parse(localStorage.getItem(this.key) || "[]"); } catch { return []; } }
    saveQueue() { try { localStorage.setItem(this.key, JSON.stringify(this.queue.slice(-150))); } catch { /* Tracking must never break navigation. */ } }
    track(eventType, data = {}) {
      if (!this.enabled) return;
      const dedupeWindows = { card_impression: 1800000, card_hover: 900000, product_view: 300000, search: 120000, category_filter: 120000, difficulty_filter: 120000, scroll_depth: 60000 };
      const signalValue = data.search_query || data.metadata?.category || data.metadata?.difficulty || data.metadata?.price || data.metadata?.scroll_percent || "";
      const signature = `${eventType}:${data.product_id || ""}:${signalValue.toString().toLowerCase()}`;
      const now = Date.now();
      if (dedupeWindows[eventType] && now - Number(this.recentSignals[signature] || 0) < dedupeWindows[eventType]) return;
      if (dedupeWindows[eventType]) {
        this.recentSignals[signature] = now;
        try { sessionStorage.setItem("smartreco_recent_signals", JSON.stringify(this.recentSignals)); } catch { /* Optional optimization. */ }
      }
      this.queue.push({ event_id: randomId(), session_id: this.sessionId, event_type: eventType,
        product_id: data.product_id || null, search_query: data.search_query || null,
        metadata: data.metadata || {}, occurred_at: new Date().toISOString() });
      this.saveQueue();
      if (this.queue.length >= 15) this.flush();
    }
    async flush(unloading = false) {
      if (!this.enabled || !this.queue.length || this.flushing) return;
      this.flushing = true;
      const batch = this.queue.slice(0, 50);
      const body = JSON.stringify({ events: batch });
      try {
        if (unloading && navigator.sendBeacon) {
          const sent = navigator.sendBeacon("/api/events/batch", new Blob([body], { type: "application/json" }));
          if (sent) { this.queue.splice(0, batch.length); this.saveQueue(); return; }
        }
        const response = await fetch("/api/events/batch", { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: unloading, credentials: "same-origin" });
        if (response.ok) { this.queue.splice(0, batch.length); this.saveQueue(); }
      } catch { this.saveQueue(); }
      finally { this.flushing = false; }
    }
    bindLifecycle() {
      addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") this.flush(true); });
      addEventListener("pagehide", () => this.flush(true));
    }
  }
  const tracker = new SmartRecoTracker();
  window.smartRecoTracker = tracker;

  const consent = $("#consent-banner");
  if (consent) {
    if (localStorage.getItem("smartreco_consent")) consent.remove();
    $("[data-consent]", consent)?.addEventListener("click", () => {
      localStorage.setItem("smartreco_consent", "granted"); tracker.enabled = true; consent.remove();
      tracker.track("return_visit", { metadata: { source: "consent" } }); toast("Personalization is on. You remain in control.");
    });
    $("[data-decline]", consent)?.addEventListener("click", () => {
      localStorage.setItem("smartreco_consent", "declined"); tracker.enabled = false; consent.remove();
    });
  }

  $$('[data-auth-tab]').forEach((tab) => tab.addEventListener("click", () => {
    $$('[data-auth-tab]').forEach((item) => item.classList.toggle("active", item === tab));
    $("#login-form")?.classList.toggle("hidden", tab.dataset.authTab !== "login");
    $("#register-form")?.classList.toggle("hidden", tab.dataset.authTab !== "register");
  }));

  const landing = $("[data-landing]");
  if (landing) {
    const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const saveData = Boolean(navigator.connection?.saveData);
    const revealNodes = $$('[data-reveal]', landing);
    if (!reducedMotion && "IntersectionObserver" in window) {
      landing.classList.add("motion-ready");
      const revealObserver = new IntersectionObserver((entries, observer) => entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      }), { rootMargin: "0px 0px -10%", threshold: 0.08 });
      revealNodes.forEach((node) => revealObserver.observe(node));
    } else {
      revealNodes.forEach((node) => node.classList.add("is-visible"));
    }

    const explorer = $("[data-feature-explorer]", landing);
    const featureTabs = explorer ? $$('[data-feature-tab]', explorer) : [];
    const featurePanels = explorer ? $$('[data-feature-panel]', explorer) : [];
    const autoplayButton = explorer ? $("[data-feature-autoplay]", explorer) : null;
    let activeFeature = 0;
    let featureTimer = null;
    let tourInView = true;
    let tourInteracting = false;
    let tourPaused = reducedMotion || saveData;

    const renderFeature = (nextIndex, focusTab = false) => {
      activeFeature = (nextIndex + featureTabs.length) % featureTabs.length;
      featureTabs.forEach((tab, index) => {
        const selected = index === activeFeature;
        tab.classList.toggle("is-active", selected);
        tab.setAttribute("aria-selected", String(selected));
        tab.tabIndex = selected ? 0 : -1;
        if (selected && focusTab) tab.focus();
      });
      featurePanels.forEach((panel, index) => {
        const selected = index === activeFeature;
        panel.classList.toggle("is-active", selected);
        panel.setAttribute("aria-hidden", String(!selected));
      });
    };
    const updateTourControl = () => {
      if (!autoplayButton) return;
      autoplayButton.hidden = reducedMotion || saveData;
      autoplayButton.setAttribute("aria-pressed", String(tourPaused));
      autoplayButton.innerHTML = tourPaused
        ? '<span aria-hidden="true">▶</span> Resume tour'
        : '<span aria-hidden="true">Ⅱ</span> Pause tour';
      explorer.classList.toggle("tour-paused", tourPaused || tourInteracting || !tourInView || document.hidden);
    };
    const scheduleFeatureTour = () => {
      clearTimeout(featureTimer);
      updateTourControl();
      if (tourPaused || tourInteracting || !tourInView || document.hidden || featureTabs.length < 2) return;
      featureTimer = setTimeout(() => {
        renderFeature(activeFeature + 1);
        scheduleFeatureTour();
      }, 6000);
    };

    featureTabs.forEach((tab, index) => {
      tab.addEventListener("click", () => { renderFeature(index); scheduleFeatureTour(); });
      tab.addEventListener("keydown", (event) => {
        const keyTargets = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
        let nextIndex = keyTargets[event.key] === undefined ? null : activeFeature + keyTargets[event.key];
        if (event.key === "Home") nextIndex = 0;
        if (event.key === "End") nextIndex = featureTabs.length - 1;
        if (nextIndex === null) return;
        event.preventDefault();
        renderFeature(nextIndex, true);
        scheduleFeatureTour();
      });
    });
    autoplayButton?.addEventListener("click", () => {
      tourPaused = !tourPaused;
      scheduleFeatureTour();
    });
    explorer?.addEventListener("pointerenter", () => { tourInteracting = true; scheduleFeatureTour(); });
    explorer?.addEventListener("pointerleave", () => { tourInteracting = false; scheduleFeatureTour(); });
    explorer?.addEventListener("focusin", () => { tourInteracting = true; scheduleFeatureTour(); });
    explorer?.addEventListener("focusout", () => setTimeout(() => {
      tourInteracting = explorer.contains(document.activeElement);
      scheduleFeatureTour();
    }));
    if (explorer && "IntersectionObserver" in window) {
      const tourObserver = new IntersectionObserver(([entry]) => {
        tourInView = entry.isIntersecting;
        scheduleFeatureTour();
      }, { threshold: 0.28 });
      tourObserver.observe(explorer);
    }
    addEventListener("visibilitychange", scheduleFeatureTour);
    renderFeature(0);
    scheduleFeatureTour();

    const decisionGraph = $("[data-decision-graph]", landing);
    const decisionNodes = decisionGraph ? $$('[data-decision-node]', decisionGraph) : [];
    let activeDecisionNode = 0;
    let decisionTimer = null;
    let decisionGraphInView = !(decisionGraph && "IntersectionObserver" in window);
    let decisionGraphInteracting = false;
    const renderDecisionNode = (nextIndex) => {
      if (!decisionNodes.length) return;
      activeDecisionNode = (nextIndex + decisionNodes.length) % decisionNodes.length;
      decisionNodes.forEach((node, index) => {
        const current = index === activeDecisionNode;
        node.classList.toggle("is-current", current);
        node.classList.toggle("is-past", index < activeDecisionNode);
        if (current) node.setAttribute("aria-current", "step");
        else node.removeAttribute("aria-current");
      });
    };
    const scheduleDecisionGraph = () => {
      clearTimeout(decisionTimer);
      if (reducedMotion || saveData || decisionGraphInteracting || !decisionGraphInView || document.hidden || decisionNodes.length < 2) return;
      decisionTimer = setTimeout(() => {
        renderDecisionNode(activeDecisionNode + 1);
        scheduleDecisionGraph();
      }, 1450);
    };
    decisionNodes.forEach((node, index) => {
      node.addEventListener("pointerenter", () => {
        decisionGraphInteracting = true;
        renderDecisionNode(index);
        scheduleDecisionGraph();
      });
      node.addEventListener("focus", () => {
        decisionGraphInteracting = true;
        renderDecisionNode(index);
        scheduleDecisionGraph();
      });
    });
    decisionGraph?.addEventListener("pointerleave", () => {
      decisionGraphInteracting = false;
      scheduleDecisionGraph();
    });
    decisionGraph?.addEventListener("focusout", () => setTimeout(() => {
      decisionGraphInteracting = decisionGraph.contains(document.activeElement);
      scheduleDecisionGraph();
    }));
    if (decisionGraph && "IntersectionObserver" in window) {
      const graphObserver = new IntersectionObserver(([entry]) => {
        decisionGraphInView = entry.isIntersecting;
        scheduleDecisionGraph();
      }, { threshold: 0.2 });
      graphObserver.observe(decisionGraph);
    }
    addEventListener("visibilitychange", scheduleDecisionGraph);
    renderDecisionNode(0);
    scheduleDecisionGraph();
  }

  const sidebar = $(".app-sidebar, .admin-sidebar");
  $(".mobile-menu")?.addEventListener("click", () => sidebar?.classList.toggle("open"));
  const learnerWorkspace = $(".app-sidebar")?.closest(".workspace-shell");
  const setSidebar = (expanded) => {
    if (!learnerWorkspace) return;
    learnerWorkspace.classList.toggle("sidebar-collapsed", !expanded);
    sidebar?.classList.toggle("collapsed", !expanded);
    $$('[data-toggle-sidebar]').forEach((button) => {
      button.setAttribute("aria-expanded", String(expanded));
      button.setAttribute("aria-label", expanded ? "Collapse navigation" : "Expand navigation");
      button.title = expanded ? "Collapse navigation" : "Expand navigation";
      const arrow = $(".collapse-arrow", button);
      if (arrow) arrow.textContent = expanded ? "←" : "→";
    });
    localStorage.setItem("smartreco_sidebar_expanded", String(expanded));
  };
  $$('[data-toggle-sidebar]').forEach((button) => button.addEventListener("click", () => {
    if (innerWidth <= 900) { sidebar?.classList.remove("open"); return; }
    setSidebar(button.getAttribute("aria-expanded") === "false");
  }));
  if (learnerWorkspace && localStorage.getItem("smartreco_sidebar_expanded") === "false" && innerWidth > 900) setSidebar(false);
  const workspace = $("[data-workspace]");
  const insightPanel = $(".insight-panel");
  const setInsights = (expanded) => {
    if (!workspace || !insightPanel) return;
    workspace.classList.toggle("twin-collapsed", !expanded);
    if (innerWidth <= 1250) insightPanel.classList.toggle("open", expanded);
    $$('[data-toggle-insights]').forEach((button) => button.setAttribute("aria-expanded", String(expanded)));
    localStorage.setItem("smartreco_twin_expanded", String(expanded));
  };
  $$('[data-toggle-insights]').forEach((button) => button.addEventListener("click", () => {
    setInsights(button.getAttribute("aria-expanded") === "false");
  }));
  if (workspace && (localStorage.getItem("smartreco_twin_expanded") === "false" || innerWidth <= 1250)) setInsights(false);

  const readIds = (key) => {
    try {
      const value = JSON.parse(localStorage.getItem(key) || "[]");
      return new Set(Array.isArray(value) ? value.filter((id) => typeof id === "string" && id) : []);
    } catch { return new Set(); }
  };
  const readPurchasedIds = () => {
    try {
      const value = JSON.parse(document.body.dataset.purchasedProductIds || "[]");
      return new Set(Array.isArray(value) ? value.filter((id) => typeof id === "string" && id) : []);
    } catch { return new Set(); }
  };
  const persistIds = (key, ids) => {
    try { localStorage.setItem(key, JSON.stringify([...ids])); }
    catch { /* Storage restrictions must not break cart actions. */ }
  };
  const savedIds = readIds("smartreco_saved");
  const cartIds = readIds("smartreco_cart");
  const purchasedIds = readPurchasedIds();
  const parseIdList = (value) => {
    try {
      const ids = JSON.parse(value || "[]");
      return Array.isArray(ids) ? ids.filter((id) => typeof id === "string" && id) : [];
    } catch { return []; }
  };
  const bundleComponents = new Map();
  $$('[data-product-id][data-component-ids]').forEach((node) => {
    const id = node.dataset.productId;
    if (id) bundleComponents.set(id, parseIdList(node.dataset.componentIds));
  });
  const normalizeCart = () => {
    let changed = false;
    purchasedIds.forEach((id) => { if (cartIds.delete(id)) changed = true; });
    $$('[data-product-id][data-offer-eligible="false"]').forEach((node) => {
      if (cartIds.delete(node.dataset.productId)) changed = true;
    });
    bundleComponents.forEach((componentIds, bundleId) => {
      if (!cartIds.has(bundleId)) return;
      componentIds.forEach((componentId) => { if (cartIds.delete(componentId)) changed = true; });
    });
    if (changed) persistIds("smartreco_cart", cartIds);
    return changed;
  };
  normalizeCart();
  const csrfToken = $('meta[name="csrf-token"]')?.content || "";
  const recordDemoPurchase = async (productIds, source) => {
    const normalizedIds = [...new Set(productIds.filter((id) => typeof id === "string" && id))];
    if (!normalizedIds.length) throw new Error("Add at least one course before checking out.");
    const response = await fetch("/api/demo-purchases", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      credentials: "same-origin",
      body: JSON.stringify({ product_ids: normalizedIds, source }),
    });
    if (!response.ok) {
      let message = "The demo purchase could not be completed. Please try again.";
      try {
        const payload = await response.json();
        if (typeof payload.detail === "string" && payload.detail) message = payload.detail;
      } catch { /* A non-JSON error response keeps the fallback message. */ }
      throw new Error(message);
    }
    try { return await response.json(); }
    catch { return {}; }
  };
  const rememberPurchaseNotice = (result) => {
    const payable = Number(result?.payable_total);
    const credit = Number(result?.ownership_credit_total);
    let message = Number.isFinite(payable)
      ? `Demo purchase completed for ₹${formatInr(payable)}.`
      : "Demo purchase completed.";
    if (Number.isFinite(credit) && credit > 0) {
      message += ` ₹${formatInr(credit)} owned-course credit applied.`;
    }
    try { sessionStorage.setItem("smartreco_purchase_notice", message); }
    catch { /* The durable purchase has already completed. */ }
  };
  const updateCartCount = () => $$('[data-cart-count]').forEach((node) => node.textContent = cartIds.size);
  const renderSavedButtons = (id) => $$('[data-save]').filter((button) => (button.dataset.productId || button.closest(".course-card")?.dataset.productId) === id).forEach((button) => {
    const saved = savedIds.has(id);
    const isHeart = button.hasAttribute("data-save-heart");
    button.classList.toggle("saved", saved);
    button.textContent = isHeart ? (saved ? "♥" : "♡") : (saved ? "Saved ✓" : "Save for later");
    button.setAttribute("aria-pressed", String(saved));
    if (isHeart) button.setAttribute("aria-label", saved ? "Remove from saved courses" : "Save course");
  });
  updateCartCount();

  $$('[data-save]').forEach((button) => {
    const card = button.closest(".course-card");
    const id = button.dataset.productId || card?.dataset.productId;
    if (!id) return;
    renderSavedButtons(id);
    button.addEventListener("click", (event) => {
      event.preventDefault(); event.stopPropagation();
      if (savedIds.has(id)) { savedIds.delete(id); tracker.track("wishlist_remove", { product_id: id }); toast("Removed from saved courses"); }
      else { savedIds.add(id); tracker.track("wishlist_add", { product_id: id }); toast("Saved for later"); }
      persistIds("smartreco_saved", savedIds); renderSavedButtons(id);
      if ($('[data-collection-grid="saved"]')) { card.remove(); if (!$(".collection-visible", collectionGrid)) $("[data-collection-empty]")?.classList.remove("hidden"); }
    });
  });
  $$('[data-cart]').forEach((button) => {
    const card = button.closest("[data-product-id]"); const id = card?.dataset.productId;
    if (!id) return;
    const defaultLabel = button.dataset.cartLabel || "Add to cart";
    const render = () => { button.classList.toggle("saved", cartIds.has(id)); button.textContent = cartIds.has(id) ? "In cart ✓" : defaultLabel; };
    render();
    button.addEventListener("click", (event) => {
      event.preventDefault(); event.stopPropagation();
      const removing = cartIds.has(id);
      let consolidated = false;
      if (removing) cartIds.delete(id);
      else {
        const containingBundle = [...bundleComponents].find(([bundleId, componentIds]) => (
          cartIds.has(bundleId) && componentIds.includes(id)
        ));
        if (containingBundle) {
          toast("This course is already included in a bundle in your cart.");
          return;
        }
        parseIdList(card.dataset.componentIds).forEach((componentId) => {
          if (cartIds.delete(componentId)) consolidated = true;
        });
        cartIds.add(id);
      }
      persistIds("smartreco_cart", cartIds); updateCartCount(); render();
      const isJourneyAction = card.hasAttribute("data-nba-card");
      tracker.track("cta_click", { product_id: id, metadata: { source: removing ? "remove_from_cart" : (isJourneyAction ? "journey_twin_add_to_cart" : "add_to_cart"), decision_id: card.dataset.decisionId, action_type: card.dataset.actionType, persuasion_strategy: card.dataset.persuasionStrategy } }); toast(removing ? "Course removed from cart" : consolidated ? "Bundle added and included course replaced in your cart" : "Course added to cart");
      if (removing && $('[data-collection-grid="cart"]')) location.reload();
    });
  });
  $$('[data-course-url]').forEach((card) => {
    const openCourse = () => {
      tracker.track("search_click", { product_id: card.dataset.productId, metadata: { source: "catalog_card" } });
      location.href = card.dataset.courseUrl;
    };
    card.addEventListener("click", (event) => {
      if (event.target.closest("a,button,input,select,textarea")) return;
      openCourse();
    });
    card.addEventListener("keydown", (event) => {
      if (event.target === card && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault(); openCourse();
      }
    });
  });
  $$('[data-buy]').forEach((link) => link.addEventListener("click", () => {
    const card = link.closest("[data-product-id]"); const id = card?.dataset.productId;
    if (id) tracker.track("cta_click", { product_id: id, metadata: { source: card.hasAttribute("data-nba-card") ? "journey_twin_buy_now" : "buy_now", decision_id: card.dataset.decisionId, action_type: card.dataset.actionType, persuasion_strategy: card.dataset.persuasionStrategy } });
  }));

  const collectionGrid = $('[data-collection-grid]');
  if (collectionGrid) {
    const type = collectionGrid.dataset.collectionGrid;
    const ids = type === "cart" ? cartIds : savedIds;
    const cards = $$(".course-card", collectionGrid);
    cards.forEach((card) => card.classList.toggle("collection-visible", ids.has(card.dataset.productId)));
    const visibleCards = cards.filter((card) => ids.has(card.dataset.productId));
    $("[data-collection-empty]")?.classList.toggle("hidden", visibleCards.length > 0);
    if (type === "cart") {
        const payableTotal = visibleCards.reduce((sum, card) => sum + Number(card.dataset.price), 0);
        const catalogTotal = visibleCards.reduce(
          (sum, card) => sum + Number(card.dataset.catalogPrice || card.dataset.price), 0,
        );
        const ownershipCredit = visibleCards.reduce(
          (sum, card) => sum + Number(card.dataset.ownershipCredit || 0), 0,
        );
        $("[data-cart-items]").textContent = String(visibleCards.length);
        $("[data-cart-subtotal]").textContent = `₹${formatInr(catalogTotal)}`;
        $("[data-cart-total]").textContent = `₹${formatInr(payableTotal)}`;
        $("[data-cart-credit]").textContent = `−₹${formatInr(ownershipCredit)}`;
        $("[data-cart-credit-row]").hidden = ownershipCredit <= 0;
    }
  }
  $("[data-checkout]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await recordDemoPurchase([...cartIds], "cart_checkout");
      rememberPurchaseNotice(result);
      cartIds.clear(); persistIds("smartreco_cart", cartIds); updateCartCount();
      location.href = "/my-courses";
    } catch (error) {
      toast(error.message, "error");
      button.disabled = false;
    }
  });

  const discovery = $(".discovery-content");
  if (discovery) {
    const pageStarted = Date.now();
    let maxScroll = 0;
    tracker.track("return_visit", { metadata: { source: "discover" } });
    const reportScroll = throttle(() => {
      const scrollable = document.documentElement.scrollHeight - innerHeight;
      const percent = scrollable ? Math.round((scrollY / scrollable) * 100) : 100;
      if (percent >= maxScroll + 20) { maxScroll = Math.min(100, Math.ceil(percent / 20) * 20); tracker.track("scroll_depth", { metadata: { scroll_percent: maxScroll } }); }
    }, 800);
    addEventListener("scroll", reportScroll, { passive: true });
    addEventListener("pagehide", () => {
      const seconds = Math.round((Date.now() - pageStarted) / 1000);
      if (seconds >= 10) tracker.track("time_spent", { metadata: { dwell_seconds: Math.min(seconds, 3600), source: "discover" } });
    });

    const impressionObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries, observer) => entries.forEach((entry) => {
      if (entry.isIntersecting && entry.intersectionRatio >= .55) {
        tracker.track("card_impression", { product_id: entry.target.dataset.productId, metadata: { position: $$(".course-card").indexOf(entry.target) } });
        observer.unobserve(entry.target);
      }
    }), { threshold: .55 }) : null;
    $$(".course-card").forEach((card) => impressionObserver?.observe(card));
    const nextActionCard = $("[data-nba-card]");
    if (nextActionCard && "IntersectionObserver" in window) {
      const nextActionObserver = new IntersectionObserver((entries, observer) => entries.forEach((entry) => {
        if (!entry.isIntersecting || entry.intersectionRatio < .55) return;
        tracker.track("recommendation_impression", { product_id: entry.target.dataset.productId || null, metadata: { recommendation_id: $("[data-recommendation-id]")?.dataset.recommendationId, decision_id: entry.target.dataset.decisionId, action_type: entry.target.dataset.actionType, persuasion_strategy: entry.target.dataset.persuasionStrategy } });
        observer.unobserve(entry.target);
      }), { threshold: .55 });
      nextActionObserver.observe(nextActionCard);
    }
    const hovered = new Set();
    $$(".course-card").forEach((card) => card.addEventListener("mouseenter", throttle(() => {
      if (hovered.has(card.dataset.productId)) return;
      hovered.add(card.dataset.productId); tracker.track("card_hover", { product_id: card.dataset.productId });
    }, 1000)));

    const search = $("#course-search");
    search?.addEventListener("input", debounce(() => {
      if (search.value.trim().length >= 2) tracker.track("search", { search_query: search.value.trim(), metadata: { source: "discover" } });
    }, 650));
    $$("[data-course-link]").forEach((link) => link.addEventListener("click", () => tracker.track("search_click", { product_id: link.closest("[data-product-id]")?.dataset.productId, metadata: { source: search?.value ? "search" : (link.closest("[data-nba-card]") ? "journey_twin" : "catalog"), decision_id: link.closest("[data-nba-card]")?.dataset.decisionId } })));
    $("#sort-select")?.addEventListener("change", (event) => {
      tracker.track("sort_change", { metadata: { sort: event.target.value } });
      const grid = $("#course-grid");
      const cards = $$(".course-card", grid);
      if (event.target.value.startsWith("price")) cards.sort((a, b) => (Number(a.dataset.price) - Number(b.dataset.price)) * (event.target.value === "price-high" ? -1 : 1)).forEach((card) => grid.append(card));
    });
    $(".filter-drawer form")?.addEventListener("submit", (event) => {
      const data = Object.fromEntries(new FormData(event.currentTarget));
      if (data.category) tracker.track("category_filter", { metadata: { category: data.category } });
      if (data.difficulty) tracker.track("difficulty_filter", { metadata: { difficulty: data.difficulty } });
    });
    $("[data-filter-toggle]")?.addEventListener("click", () => $("#filter-drawer")?.classList.toggle("open"));

    $$('[data-generate]').forEach((button) => button.addEventListener("click", async () => {
      button.disabled = true; const original = button.textContent; button.textContent = "Understanding your journey…";
      try { const response = await fetch("/api/recommendations/generate", { method: "POST" }); if (!response.ok) throw new Error(); toast("Your recommendation path is refreshed."); location.reload(); }
      catch { toast("We couldn't refresh your path just now.", "error"); button.disabled = false; button.textContent = original; }
    }));
    $("[data-dismiss-story]")?.addEventListener("click", (event) => { event.currentTarget.closest(".recommendation-story").remove(); tracker.track("recommendation_dismiss", { metadata: { recommendation_id: $("[data-recommendation-id]")?.dataset.recommendationId } }); });

    $$('[data-feedback]').forEach((button) => button.addEventListener("click", async () => {
      const card = button.closest(".course-card"); const recommendationId = $("[data-recommendation-id]")?.dataset.recommendationId;
      await fetch("/api/recommendations/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ product_id: card.dataset.productId, recommendation_id: recommendationId, feedback_type: button.dataset.feedback }) });
      card.remove(); toast("Feedback recorded. Future rankings will adapt.");
    }));
    $("[data-nba-dismiss]")?.addEventListener("click", async (event) => {
      const card = event.currentTarget.closest("[data-nba-card]");
      const response = await fetch(`/api/next-best-action/${card.dataset.decisionId}/feedback`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outcome: "not_interested" }),
      });
      if (response.ok) { card.remove(); toast("Understood. SmartReco will adapt and ease off this path."); }
      else toast("Feedback could not be saved.", "error");
    });

    const whyDialog = $("#why-dialog");
    $$('[data-why]').forEach((button) => button.addEventListener("click", () => {
      const card = button.closest(".course-card"); const details = $(".match-details", card);
      $("#why-dialog-content").innerHTML = `<div class="dialog-heading"><span>VERIFIED MATCH EVIDENCE</span><h2>${card.dataset.title}</h2><p>${$("p", details)?.textContent || "Matched to your current profile."}</p></div>${$("ul", details)?.outerHTML || ""}<p>Evidence is derived only from your own stored learning interactions.</p>`;
      whyDialog.showModal(); tracker.track("recommendation_click", { product_id: card.dataset.productId, metadata: { source: "why_this" } });
    }));

    $("[data-save-privacy]")?.addEventListener("click", async () => {
      const payload = { personalization_enabled: $("#personalization-toggle").checked,
        email_digest_enabled: $("#digest-toggle").checked, preferred_hour: Number($("#digest-hour").value),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" };
      const response = await fetch("/api/preferences", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      if (response.ok) { tracker.enabled = payload.personalization_enabled && localStorage.getItem("smartreco_consent") === "granted"; toast("Privacy and delivery settings saved."); }
      else toast("Settings could not be saved.", "error");
    });
    $("[data-erase-behavior]")?.addEventListener("click", async () => {
      if (!confirm("Erase your stored behavior, Journey Twin, and recommendations? This cannot be undone.")) return;
      const response = await fetch("/api/behavior", { method: "DELETE" });
      if (response.ok) { localStorage.removeItem("smartreco_event_queue_v1"); toast("Behavior history erased."); setTimeout(() => location.reload(), 700); }
      else toast("History could not be erased.", "error");
    });
  }

  const courseDetail = $("[data-track-product]");
  if (courseDetail) {
    const productId = courseDetail.dataset.trackProduct; const started = Date.now(); let reportedSeconds = 0;
    tracker.track("product_view", { product_id: productId, metadata: { source: "course_detail" } });
    const reportDwell = () => { const seconds = Math.min(3600, Math.round((Date.now() - started) / 1000)); const delta = seconds - reportedSeconds; if (delta >= 8) { tracker.track("time_spent", { product_id: productId, metadata: { dwell_seconds: delta, source: "course_detail" } }); reportedSeconds = seconds; } };
    [30000, 120000, 300000].forEach((delay) => setTimeout(() => { reportDwell(); tracker.flush(); }, delay));
    addEventListener("pagehide", () => { reportDwell(); tracker.flush(true); });
    const purchaseButtons = $$('[data-demo-purchase]');
    purchaseButtons.forEach((button) => button.addEventListener("click", async () => {
      purchaseButtons.forEach((item) => { item.disabled = true; });
      try {
        const result = await recordDemoPurchase([button.dataset.productId], "course_detail_demo_purchase");
        rememberPurchaseNotice(result);
        location.href = "/my-courses";
      } catch (error) {
        toast(error.message, "error");
        purchaseButtons.forEach((item) => { item.disabled = false; });
      }
    }));
  }

  const admin = $(".admin-shell");
  if (admin) {
    const showAdminView = (id) => { $$('[data-admin-tab]').forEach((button) => button.classList.toggle("active", button.dataset.adminTab === id)); $$('[data-admin-view]').forEach((view) => view.classList.toggle("active", view.dataset.adminView === id)); history.replaceState(null, "", `#${id}`); };
    $$('[data-admin-tab]').forEach((button) => button.addEventListener("click", () => showAdminView(button.dataset.adminTab)));
    if (location.hash) showAdminView(location.hash.slice(1));
    const productDialog = $("#product-dialog"); const productForm = $("#product-form"); const archiveForm = $("#archive-form");
    $("[data-product-create]")?.addEventListener("click", () => { productForm.reset(); productForm.action = "/admin/products"; archiveForm.classList.add("hidden"); $("#product-dialog-title").textContent = "Create a course"; productDialog.showModal(); });
    $$('[data-product-edit]').forEach((button) => button.addEventListener("click", () => { const product = JSON.parse(button.dataset.product); productForm.action = `/admin/products/${product.id}`; Object.entries(product).forEach(([key, value]) => { const field = productForm.elements[key]; if (!field) return; if (field.type === "checkbox") field.checked = Boolean(value); else field.value = value ?? ""; }); archiveForm.action = `/admin/products/${product.id}/archive`; archiveForm.classList.remove("hidden"); $("#product-dialog-title").textContent = `Edit ${product.title}`; productDialog.showModal(); }));

    let replay = { events: [], runs: [], position: -1, timer: null };
    const loadReplay = async () => { const userId = $("#replay-user")?.value; if (!userId) return; const response = await fetch(`/admin/api/replay/${userId}`); replay = { ...await response.json(), position: -1, timer: null }; renderReplay(); };
    const renderReplay = () => { const count = replay.events.length; const position = replay.position; $("#replay-position").textContent = `${Math.max(0, position + 1)} / ${count}`; $("#replay-progress").style.width = `${count ? ((position + 1) / count) * 100 : 0}%`;
      if (position < 0 || !replay.events[position]) { $("#replay-event").innerHTML = "<span>⌁</span><h2>Ready to replay</h2><p>Select play to step through the learner's real behavioral timeline.</p>"; return; }
      const event = replay.events[position]; $("#replay-event").innerHTML = `<span>${event.type === "search" ? "⌕" : "⌁"}</span><h2>${event.type.replaceAll("_", " ")}</h2><p>${event.query || event.product_id || "Marketplace signal"}</p><small>${new Date(event.at).toLocaleString()}</small>`;
      const progress = Math.round(((position + 1) / count) * 100); const closestRun = replay.runs[Math.min(replay.runs.length - 1, Math.floor(progress / Math.max(1, 100 / replay.runs.length)))];
      $("#replay-inspector").innerHTML = `<dl class="twin-facts"><div><dt>Signals replayed</dt><dd>${position + 1}</dd></div><div><dt>Journey progress</dt><dd>${progress}%</dd></div><div><dt>Agent runs</dt><dd>${replay.runs.length}</dd></div></dl>${closestRun ? `<h3>Latest agent state</h3><p>${closestRun.trigger} · ${closestRun.status}</p><p>${closestRun.query || "Waiting for retrieval trigger"}</p><div class="node-trace">${closestRun.trace.map((node) => `<span>${node.node}</span>`).join("")}</div>` : "<p>Recommendation trigger not reached yet.</p>"}`;
    };
    const stepReplay = (amount) => { replay.position = Math.max(-1, Math.min(replay.events.length - 1, replay.position + amount)); renderReplay(); };
    $("#replay-user")?.addEventListener("change", loadReplay); $("[data-replay-prev]")?.addEventListener("click", () => stepReplay(-1)); $("[data-replay-next]")?.addEventListener("click", () => stepReplay(1));
    $("[data-replay-play]")?.addEventListener("click", (event) => { if (replay.timer) { clearInterval(replay.timer); replay.timer = null; event.currentTarget.textContent = "▶ Play journey"; return; } event.currentTarget.textContent = "Ⅱ Pause"; if (replay.position >= replay.events.length - 1) replay.position = -1; replay.timer = setInterval(() => { stepReplay(1); if (replay.position >= replay.events.length - 1) { clearInterval(replay.timer); replay.timer = null; event.currentTarget.textContent = "▶ Replay journey"; } }, 1200); });
    loadReplay();
  }
})();
