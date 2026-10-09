import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { call, clearSession, createChatConversation, loadChatConversation, loadAuthenticatedUser, loadRetailerProducts, saveRetailerProduct, streamPricingWorkflow, acceptRecommendation, sendChatMessage } from './api.js';
import { toggleProductList } from './productListState.js';
import { appendChatMessage, clearChatSession, getActiveChatConversation, getChatConversation, rememberChatConversation, restoreChatMessages } from './chatState.js';
import { createEmptyForgotPasswordState, createEmptyLoginCredentials, createEmptyResetPasswordState, createSubmissionLock, forgotPasswordNotice, INVALID_RESET_LINK_MESSAGE, passwordResetErrorMessage, readResetToken, submitPasswordReset } from './authState.js';
import './styles.css';

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000';

function PricePulseAssistantMark({ size = 32 }) {
  return <svg className="assistant-mark" width={size} height={size} viewBox="0 0 40 40" role="img" aria-label="PricePulse Assistant">
    <circle cx="20" cy="20" r="19" fill="#1b704d" />
    <path d="M7 21h7l3.2-7.5L23 27l4-9 2.2 3H34" fill="none" stroke="#e1f1a8" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>;
}

function Auth({ onLogin }) {
  const isResetRoute = window.location.pathname.replace(/\/+$/, '') === '/reset-password';
  const initialResetToken = readResetToken(window.location.search);
  const [screen, setScreen] = useState(initialResetToken || isResetRoute ? 'reset' : 'login');
  const [resetToken, setResetToken] = useState(initialResetToken);
  const [credentials, setCredentials] = useState(createEmptyLoginCredentials);
  const [forgotPassword, setForgotPassword] = useState(createEmptyForgotPasswordState);
  const [resetPasswords, setResetPasswords] = useState(createEmptyResetPasswordState);
  const [show, setShow] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const submissionLock = useRef(null);
  if (submissionLock.current === null) submissionLock.current = createSubmissionLock();
  useEffect(() => {
    if (!initialResetToken) return;
    const cleanUrl = new URL(window.location.href);
    cleanUrl.searchParams.delete('token');
    cleanUrl.searchParams.delete('reset_token');
    window.history.replaceState({}, '', `${cleanUrl.pathname}${cleanUrl.search}${cleanUrl.hash}`);
  }, []);
  function showScreen(nextScreen) {
    if ((nextScreen === 'login' || nextScreen === 'forgot') && window.location.pathname.replace(/\/+$/, '') === '/reset-password') {
      window.history.replaceState({}, '', '/');
    }
    setCredentials(createEmptyLoginCredentials());
    setForgotPassword(createEmptyForgotPasswordState());
    setResetPasswords(createEmptyResetPasswordState());
    setResetToken(''); setScreen(nextScreen);
    setError(''); setNotice(''); setShow(false);
  }

  async function submit(e) {
    e.preventDefault();
    if (!submissionLock.current.acquire()) return;
    setBusy(true); setError(''); setNotice(''); const element = e.currentTarget; const form = new FormData(element);
    try {
      if (screen === 'forgot') {
        const result = await call('/auth/forgot-password', { method: 'POST', body: JSON.stringify({ email: forgotPassword.email }) });
        setNotice(forgotPasswordNotice(result));
        return;
      }
      if (screen === 'reset') {
        await submitPasswordReset(resetToken, resetPasswords, (token, newPassword) => call('/auth/reset-password', { method: 'POST', body: JSON.stringify({ token, new_password: newPassword }) }));
        setResetToken(''); setForgotPassword(createEmptyForgotPasswordState()); setResetPasswords(createEmptyResetPasswordState()); setCredentials(createEmptyLoginCredentials());
        setScreen('reset-success');
        return;
      }
      if (screen === 'register') await call('/auth/register', { method: 'POST', body: JSON.stringify({ retailer_name: form.get('name'), email: form.get('email'), business_name: form.get('business') || null, password: form.get('password') }) });
      const body = new URLSearchParams({ username: form.get('email'), password: form.get('password') });
      const response = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Sign in failed. Check your email and password.'); onLogin(data.access_token);
    } catch (err) { if (screen === 'login') { element.reset(); setCredentials(createEmptyLoginCredentials()); } setError(screen === 'reset' ? passwordResetErrorMessage(err) : err.message); } finally { submissionLock.current.release(); setBusy(false); }
  }
  const isLogin = screen === 'login';
  const isRegister = screen === 'register';
  const title = isRegister ? 'Create your account' : screen === 'forgot' ? 'Forgot Password?' : screen === 'reset' ? 'Reset Password' : screen === 'reset-success' ? 'Password reset successfully' : 'Good to see you.';
  return <main className="auth-shell"><section className="auth-art"><div className="brand"><PricePulseAssistantMark size={30} /> PricePulse Assistant</div><div className="art-copy"><span className="eyebrow">PRICING INTELLIGENCE, IN MOTION</span><h1>Find the price<br />that moves you <i>forward.</i></h1><p>Market context, clear recommendations, and confident decisions in one calm workspace.</p><div className="orbit"><div className="orbit-ring ring-one" /><div className="orbit-ring ring-two" /><div className="orbit-center">₹</div><span className="orbit-tag tag-a">Market signals</span><span className="orbit-tag tag-b">Your margin</span></div></div><span className="art-foot">Built for the next move.</span></section><section className="auth-panel"><form key={screen} className="auth-form" autoComplete="on" onSubmit={submit}><span className="eyebrow">{isRegister ? 'START YOUR WORKSPACE' : isLogin ? 'WELCOME BACK' : 'PRICEPULSE ACCOUNT'}</span><h2>{title}</h2>
    {screen === 'forgot' && <><p className="muted">Enter your registered email. If an account exists, we’ll send reset instructions.</p><label>Email address<input name="email" type="email" required autoComplete="email" value={forgotPassword.email} onChange={event => setForgotPassword({ email: event.target.value })} placeholder="you@company.com" /></label></>}
    {screen === 'reset' && <><p className="muted">Choose a new password for your account.</p>{!resetToken && <div className="error-msg" role="alert">{INVALID_RESET_LINK_MESSAGE}</div>}<label>New Password<input name="new_password" type="password" required autoComplete="new-password" value={resetPasswords.newPassword} onChange={event => setResetPasswords(current => ({ ...current, newPassword: event.target.value }))} placeholder="Enter a new password" /></label><label>Confirm Password<input name="confirm_password" type="password" required autoComplete="new-password" value={resetPasswords.confirmPassword} onChange={event => setResetPasswords(current => ({ ...current, confirmPassword: event.target.value }))} placeholder="Confirm your new password" /></label></>}
    {screen === 'reset-success' && <><p className="muted" role="status">Password reset successfully.<br />You can now sign in with your new password.</p><button type="button" className="primary wide" onClick={() => showScreen('login')}>Back to Sign In</button></>}
    {(isLogin || isRegister) && <>{isRegister && <><label>Your name<input name="name" required autoComplete="name" placeholder="Alex Morgan" /></label><label>Business name <small>Optional</small><input name="business" autoComplete="organization" placeholder="Studio or store" /></label></>}<label>Email address<input name="email" type="email" required autoComplete="username" value={credentials.email} onChange={event => setCredentials(current => ({ ...current, email: event.target.value }))} placeholder="you@company.com" /></label><label>Password<span className="password-field"><input name="password" type={show ? 'text' : 'password'} required autoComplete={isLogin ? 'current-password' : 'new-password'} value={credentials.password} onChange={event => setCredentials(current => ({ ...current, password: event.target.value }))} placeholder={isRegister ? '8+ characters, mixed case and symbol' : 'Enter your password'} /><button type="button" className="show-pass" onClick={() => setShow(!show)}>{show ? 'Hide' : 'Show'}</button></span></label></>}
    {error && <div className="error-msg" role="alert">{error}</div>}{notice && <div className="success-msg" role="status">{notice}</div>}
    {screen !== 'reset-success' && <button className="primary wide" disabled={busy || screen === 'reset' && !resetToken}>{busy ? screen === 'forgot' ? 'Sending…' : 'Resetting…' : isRegister ? 'Create account' : screen === 'forgot' ? 'Send Reset Link' : screen === 'reset' ? 'Reset Password' : 'Sign in'} <span>↗</span></button>}
    {isLogin && <p className="switch-auth"><button type="button" onClick={() => showScreen('forgot')}>Forgot Password?</button></p>}
    {isLogin && <p className="switch-auth">New to PricePulse? <button type="button" onClick={() => showScreen('register')}>Create account</button></p>}
    {isRegister && <p className="switch-auth">Already have an account? <button type="button" onClick={() => showScreen('login')}>Sign in</button></p>}
    {screen === 'forgot' && <p className="switch-auth"><button type="button" onClick={() => showScreen('login')}>Back to Sign In</button></p>}
    {screen === 'reset' && <p className="switch-auth"><button type="button" onClick={() => showScreen('forgot')}>Request a new reset link</button><button type="button" onClick={() => showScreen('login')}>Back to Sign In</button></p>}
    <p className="auth-legal">Your workspace is private and protected.</p></form></section></main>;
}

function App() {
  const sessionGeneration = useRef(0);
  const workflowProductId = useRef(null);
  const workflowInFlight = useRef(new Set());
  const [token, setToken] = useState(() => localStorage.getItem('pp-token') || '');
  const [user, setUser] = useState(null), [profileLoading, setProfileLoading] = useState(Boolean(localStorage.getItem('pp-token')));
  const [products, setProducts] = useState([]), [recs, setRecs] = useState([]), [selected, setSelected] = useState(null);
  const [productsVisible, setProductsVisible] = useState(false), [formOpen, setFormOpen] = useState(false), [editing, setEditing] = useState(null), [deleting, setDeleting] = useState(null);
  const [operation, setOperation] = useState(''), [error, setError] = useState(''), [analysis, setAnalysis] = useState(null), [notice, setNotice] = useState('');
  const [workflowEvents, setWorkflowEvents] = useState([]);
  const [chatOpen, setChatOpen] = useState(false), [question, setQuestion] = useState(''), [chatMessages, setChatMessages] = useState([]), [conversationId, setConversationId] = useState(null);
  const startSession = nextToken => {
    clearChatSession();
    sessionGeneration.current += 1; workflowProductId.current = null;
    setUser(null); setProfileLoading(true); setProducts([]); setRecs([]); setSelected(null); setProductsVisible(false);
    setFormOpen(false); setEditing(null); setDeleting(null); setAnalysis(null); setWorkflowEvents([]);
    setOperation(''); setError(''); setNotice(''); setChatOpen(false); setQuestion(''); setChatMessages([]); setConversationId(null);
    setToken(nextToken);
  };
  const signOut = () => {
    sessionGeneration.current += 1; workflowProductId.current = null; clearSession(); clearChatSession(); setToken(''); setUser(null); setProfileLoading(false); setProducts([]); setRecs([]); setSelected(null); setProductsVisible(false); setFormOpen(false); setEditing(null); setDeleting(null); setAnalysis(null); setWorkflowEvents([]); setOperation(''); setError(''); setNotice(''); setChatOpen(false); setQuestion(''); setChatMessages([]); setConversationId(null);
  };
  const api = (path, options = {}) => call(path, { ...options, token, onUnauthorized: signOut });
  async function toggleChat() {
    if (chatOpen) { setChatOpen(false); return; }
    setChatOpen(true);
    const generation = sessionGeneration.current;
    try {
      const savedConversation = selected ? getChatConversation(selected.id) : getActiveChatConversation();
      const conversation = savedConversation
        ? await loadChatConversation(token, savedConversation.id, signOut)
        : await createChatConversation(token, selected?.id ?? null, signOut);
      if (generation !== sessionGeneration.current) return;
      if (!selected && conversation.product_id != null) {
        const resumedProduct = await api(`/api/products/${conversation.product_id}`);
        if (generation !== sessionGeneration.current) return;
        setSelected(resumedProduct);
      }
      setConversationId(conversation.id);
      rememberChatConversation(conversation);
      setChatMessages(restoreChatMessages(conversation));
    } catch (e) {
      if (generation === sessionGeneration.current) setChatMessages([{ role: 'assistant', content: e.message }]);
    }
  }
  const recommendation = useMemo(() => recs.find(r => r.retailer_product_id === selected?.id), [recs, selected]);
  const currency = n => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(n || 0);
  const priceContext = evidence => evidence === 'LIVE' || evidence === 'STALE_FALLBACK'
    ? 'Based on relevant marketplace price evidence.'
    : 'A product and business estimate; live comparable prices were not available.';
  const decisionPrice = recommendation ? Number(recommendation.accepted_price ?? recommendation.recommended_price) : 0;
  const minimumSellingPrice = selected ? Number(selected.cost_price || 0) * (1 + Number(selected.minimum_profit_margin || 0) / 100) : 0;
  const expectedMargin = selected?.cost_price ? (decisionPrice - Number(selected.cost_price)) / Number(selected.cost_price) * 100 : 0;
  const marginHeadroom = expectedMargin - Number(selected?.minimum_profit_margin || 0);
  useEffect(() => {
    let active = true;
    if (!token) { localStorage.removeItem('pp-token'); setUser(null); setProfileLoading(false); return undefined; }
    localStorage.setItem('pp-token', token); setProfileLoading(true);
    loadAuthenticatedUser(token, signOut).then(profile => { if (active) setUser(profile); })
      .catch(e => { if (active) setError(e.message); })
      .finally(() => { if (active) setProfileLoading(false); });
    return () => { active = false; };
  }, [token]);
  async function loadProducts() {
    const generation = sessionGeneration.current;
    setOperation('load'); setError('');
    try {
      const p = await loadRetailerProducts(token, signOut);
      if (generation !== sessionGeneration.current) return;
      setProducts(p); setProductsVisible(true);
      setSelected(current => current ? p.find(x => x.id === current.id) || null : null);
      try {
        const r = await api('/api/recommendations');
        if (generation !== sessionGeneration.current) return;
        if (Array.isArray(r)) setRecs(r.map(item => ({ ...item, context: priceContext(item.evidence_type), reasoning: item.reasoning_points || [], marketPriceRangeDisplay: item.market_price_range_display || 'Not available' })));
      } catch (e) { if (generation === sessionGeneration.current) setError('Your products are ready, but saved price recommendations could not be loaded.'); }
    } catch (e) { if (generation === sessionGeneration.current) setError(e.message); } finally { if (generation === sessionGeneration.current) setOperation(''); }
  }
  function toggleProducts() {
    void toggleProductList(productsVisible, {
      close: () => setProductsVisible(false),
      open: loadProducts,
    });
  }
  async function saveProduct(e) {
    const generation = sessionGeneration.current;
    e.preventDefault(); setOperation('save'); setError(''); setNotice('');
    const f = new FormData(e.currentTarget);
    const quantityValue = f.get('quantity_value');
    const body = { product_name: f.get('product_name'), category: f.get('category'), brand: f.get('brand'), product_details: f.get('details'), cost_price: Number(f.get('cost')), stock_quantity: Number(f.get('stock')), quantity_value: quantityValue ? Number(quantityValue) : null, quantity_unit: quantityValue ? f.get('quantity_unit') : null, minimum_profit_margin: Number(f.get('margin')) };
    try {
      const p = await saveRetailerProduct(token, body, editing, signOut);
      if (generation !== sessionGeneration.current) return;
      const wasEditing = Boolean(editing);
      setProducts(current => [p, ...current.filter(item => item.id !== p.id)]); setProductsVisible(true); setSelected(p); setRecs(current => current.filter(r => r.retailer_product_id !== p.id)); setAnalysis(null); setWorkflowEvents([]); setChatMessages([]); setConversationId(null); setQuestion(''); setFormOpen(false); setEditing(null);
      setNotice(wasEditing ? `${p.product_name} was updated. Re-evaluating its pricing now.` : { title: 'Product added successfully', detail: `${p.product_name} is now in your workspace. Preparing its price recommendation.` });
      await runPricingWorkflow(p);
    } catch (e) { if (generation === sessionGeneration.current) setError(e.message); } finally { if (generation === sessionGeneration.current) setOperation(''); }
  }
  async function runPricingWorkflow(product) {
    if (!product || operation === 'workflow' || workflowInFlight.current.has(product.id)) return;
    workflowInFlight.current.add(product.id);
    const generation = sessionGeneration.current;
    workflowProductId.current = product.id;
    setOperation('workflow'); setError(''); setAnalysis(null); setWorkflowEvents([]);
    try {
      const result = await streamPricingWorkflow(product.id, token, event => {
        if (generation !== sessionGeneration.current || workflowProductId.current !== product.id) return;
        setWorkflowEvents(current => {
          const index = current.findIndex(item => item.stage === event.stage);
          if (index < 0) return [...current, event];
          return current.map((item, itemIndex) => itemIndex === index ? event : item);
        });
      }, signOut);
      if (generation !== sessionGeneration.current || workflowProductId.current !== product.id) return;
      setAnalysis(result);
      if (result.recommendation) {
        const r = result.recommendation;
        setRecs(current => [{ id: r.id, retailer_product_id: product.id, recommended_price: r.recommended_price, recommended_price_min: r.recommended_price_min, recommended_price_max: r.recommended_price_max, profit_percentage: r.profit_percentage, accepted_by_user: r.accepted_by_user, context: priceContext(r.evidence_type), reasoning: r.reasoning || [], marketPriceRangeDisplay: r.market_price_range_display || 'Not available' }, ...current.filter(x => x.retailer_product_id !== product.id)]);
        setNotice({ title: 'Product analysis completed', detail: `${product.product_name} has a new price recommendation ready for your review.` });
      } else {
        setRecs(current => current.filter(x => x.retailer_product_id !== product.id));
        setNotice('We couldn’t complete a reliable pricing recommendation. Please review the product details or try again.');
      }
      const freshProducts = await loadRetailerProducts(token, signOut);
      if (generation !== sessionGeneration.current || workflowProductId.current !== product.id) return;
      setProducts(freshProducts);
      setSelected(current => current?.id === product.id ? freshProducts.find(item => item.id === product.id) || null : current);
    } catch (e) { if (generation === sessionGeneration.current && workflowProductId.current === product.id) setError(e.message); } finally { workflowInFlight.current.delete(product.id); if (generation === sessionGeneration.current && workflowProductId.current === product.id) setOperation(''); }
  }
  async function analyze() {
    if (!selected || operation) return;
    await runPricingWorkflow(selected);
  }
  async function acceptPrice() {
    if (!recommendation?.id || recommendation.accepted_by_user || operation) return;
    const generation = sessionGeneration.current;
    setOperation('accepting'); setError('');
    try {
      const result = await acceptRecommendation(token, recommendation.id, signOut);
      if (generation !== sessionGeneration.current) return;
      if (!result.accepted || result.task_status !== 'COMPLETED') throw new Error('We couldn’t confirm that price decision. Please try again.');
      setRecs(current => current.map(item => item.id === result.recommendation_id ? { ...item, accepted_by_user: true, accepted_price: result.accepted_price } : item));
      setNotice({ title: 'Pricing task completed', detail: `Your price of ${currency(result.accepted_price)} has been accepted and saved.` });
    } catch (e) { if (generation === sessionGeneration.current) setError(e.message); } finally { if (generation === sessionGeneration.current) setOperation(''); }
  }
  async function removeProduct() {
    if (!deleting) return; setOperation('delete'); setError('');
    const generation = sessionGeneration.current;
    const productId = deleting.id;
    if (workflowProductId.current === productId) { workflowProductId.current = null; setWorkflowEvents([]); setAnalysis(null); }
    try {
      await api(`/api/products/${productId}`, { method: 'DELETE' });
      if (generation !== sessionGeneration.current) return;
      setProducts(current => current.filter(p => p.id !== productId)); setRecs(current => current.filter(r => r.retailer_product_id !== productId));
      if (selected?.id === productId) { setSelected(null); setAnalysis(null); setQuestion(''); setChatMessages([]); setConversationId(null); }
      setDeleting(null); setNotice('Product removed from your list.');
      const latest = await loadRetailerProducts(token, signOut);
      if (generation !== sessionGeneration.current) return;
      setProducts(latest);
    } catch (e) { if (generation === sessionGeneration.current) setError(e.message); } finally { if (generation === sessionGeneration.current) setOperation(''); }
  }
  async function ask(e) {
    e.preventDefault(); if (!question.trim() || operation || !conversationId) return;
    const currentQuestion = question.trim();
    setQuestion(''); setOperation('chat'); setChatMessages(current => appendChatMessage(current, { role: 'user', content: currentQuestion }));
    const generation = sessionGeneration.current;
    try {
      const result = await sendChatMessage(token, conversationId, selected?.id ?? null, currentQuestion, signOut);
      if (generation === sessionGeneration.current) {
        setConversationId(result.conversation_id);
        setChatMessages(current => appendChatMessage(current, { role: 'assistant', content: result.answer }));
        const r = result.repricing?.recommendation;
        if (r && selected) {
          setAnalysis(result.repricing);
          setRecs(current => [{ id: r.id, retailer_product_id: selected.id, recommended_price: r.recommended_price, recommended_price_min: r.recommended_price_min, recommended_price_max: r.recommended_price_max, profit_percentage: r.profit_percentage, evidence_type: r.evidence_type, accepted_by_user: r.accepted_by_user, context: priceContext(r.evidence_type), reasoning: r.reasoning || [] }, ...current.filter(item => item.retailer_product_id !== selected.id)]);
        }
      }
    } catch (e) { if (generation === sessionGeneration.current) setChatMessages(current => appendChatMessage(current, { role: 'assistant', content: e.message })); }
    finally { if (generation === sessionGeneration.current) setOperation(''); }
  }
  if (window.location.pathname.replace(/\/+$/, '') === '/reset-password' || !token) return <Auth onLogin={startSession} />;
  const editProduct = p => { setEditing(p); setFormOpen(true); setError(''); };
  return <div className="app-shell">
    <main className="main-area"><header className="topbar"><a className="brand" href="#"><PricePulseAssistantMark size={30} /><span>PricePulse Assistant</span></a><div className="header-actions"><span className="user-greeting" aria-live="polite">{profileLoading ? 'Loading your account…' : user?.retailer_name ? `Welcome, ${user.retailer_name}` : ''}</span><button className="outline" onClick={signOut}>Sign Out</button></div></header>
      <div className="page-heading"><h1>PricePulse</h1><p>Your AI-powered pricing assistant</p></div>
      {error && <div className="notice error-msg" role="alert">{error}<button onClick={() => setError('')}>×</button></div>}{notice && <div className="notice" role="status">{typeof notice === 'string' ? notice : <><b>✓ {notice.title}</b><small>{notice.detail}</small></>}<button onClick={() => setNotice('')}>×</button></div>}
      <div className="dashboard-actions"><button className="outline" onClick={toggleProducts} disabled={operation === 'load'}>{operation === 'load' ? 'Loading your products…' : productsVisible ? 'Hide My Products' : 'View My Products'}</button><button className="primary" onClick={() => { setEditing(null); setFormOpen(true); }}>Add Product <span>＋</span></button></div>
      {!selected ? (productsVisible ? null : <section className="empty-hero"><span className="empty-icon">✳</span><span className="eyebrow">YOUR PRICING WORKSPACE</span><h2>Make your next<br /><i>pricing decision clear.</i></h2><p>Add a product to get a thoughtful price recommendation, informed by your costs and available market information.</p></section>) : <>
        <section className="welcome-row"><div><span className="eyebrow">YOUR PRODUCT</span><h2>{selected.product_name}</h2><p className="muted">{selected.category || 'Product'}{selected.brand ? ` · ${selected.brand}` : ''}</p></div><div className="action-row"><button className="outline" onClick={() => editProduct(selected)}>Edit product</button><button className="outline" onClick={() => setDeleting(selected)}>Delete</button><button className="outline" onClick={() => { setSelected(null); setAnalysis(null); }}>Choose another</button></div></section>
        {workflowProductId.current === selected.id && (operation === 'workflow' || workflowEvents.length > 0) ? <section className="workflow-activity" aria-live="polite"><div className="section-heading"><div><span className="eyebrow">PRODUCT ACTIVITY</span><h3>{operation === 'workflow' ? 'Analyzing your product' : analysis?.recommendation ? 'Analysis completed' : 'Analysis update'}</h3></div>{operation === 'workflow' && <span className="progress-spinner" aria-label="In progress" />}</div>{workflowEvents.length ? <ol>{workflowEvents.map((event, index) => <li className={`workflow-event ${event.status}`} key={`${event.stage}-${index}`}><span className="event-mark">{event.status === 'completed' ? '✓' : event.status === 'failed' ? '!' : <i />}</span><span>{event.message}</span></li>)}</ol> : <p>Starting product analysis…</p>}{!operation && analysis?.recommendation && <p className="workflow-complete">✓ Product analysis completed</p>}</section> : null}
        <section className="summary-grid"><article className="recommend-card"><div className="card-top"><span className="eyebrow">{recommendation?.accepted_by_user ? 'ACCEPTED PRICE' : 'RECOMMENDED SELLING PRICE'}</span></div>{recommendation ? <><strong className="price-value">{currency(recommendation.accepted_price ?? recommendation.recommended_price)}</strong>{recommendation.recommended_price_min != null && recommendation.recommended_price_max != null && <p className="price-range">Suggested range: {currency(recommendation.recommended_price_min)}–{currency(recommendation.recommended_price_max)}</p>}<p className="muted">{recommendation.accepted_by_user ? '✓ Price accepted · Pricing task completed' : (recommendation.context || 'A starting point for your next pricing decision.')}</p><p className="muted">Pricing basis: {recommendation.evidence_type === 'LIVE' || recommendation.evidence_type === 'STALE_FALLBACK' ? 'Market evidence' : 'Product and business estimate'}</p><dl className="price-decision-facts"><div><dt>Minimum selling price</dt><dd>{currency(minimumSellingPrice)}</dd></div><div><dt>Expected gross profit per pack</dt><dd>{currency(decisionPrice - Number(selected.cost_price || 0))}</dd></div><div><dt>Expected margin on cost</dt><dd>{expectedMargin.toFixed(1)}%</dd></div><div><dt>Headroom above minimum</dt><dd>{marginHeadroom.toFixed(1)} percentage points</dd></div><div><dt>Comparable market range</dt><dd>{recommendation.marketPriceRangeDisplay || 'Not available'}</dd></div></dl><div className="why-price"><b>Why this price?</b><ul>{(Array.isArray(recommendation.reasoning) ? recommendation.reasoning : []).slice(0, 4).map((item, index) => <li key={index}>{item}</li>)}</ul></div>{recommendation.accepted_by_user ? <div className="accepted-confirmation" role="status">✓ Recommendation saved<br />✓ Product pricing task completed</div> : <button className="primary accept-price" disabled={operation === 'accepting'} onClick={acceptPrice}>{operation === 'accepting' ? 'Saving your pricing decision…' : 'Accept Price'}</button>}</> : <div className="card-empty"><span>✳</span><p>{operation === 'workflow' ? 'The pricing workflow is checking your product and available market information.' : analysis ? 'We couldn’t complete a reliable market-based recommendation.' : 'Review available market information and get a price that respects your minimum margin.'}</p><button className="primary" disabled={operation !== ''} onClick={analyze}>{operation === 'workflow' ? 'Analyzing…' : 'Analyze pricing'} <span>↗</span></button></div>}</article>
          <article className="landscape-card product-summary"><div className="card-top"><span className="eyebrow">PRODUCT DETAILS</span></div><div className="product-facts"><div><span>Cost price</span><b>{currency(selected.cost_price)}</b></div><div><span>Pack size</span><b>{selected.quantity_value ? `${selected.quantity_value} ${selected.quantity_unit}` : 'Not specified'}</b></div><div><span>In stock</span><b>{selected.stock_quantity} packs</b></div><div><span>Minimum profit margin</span><b>{Number(selected.minimum_profit_margin || 0)}%</b></div></div><button className="text-action" disabled={operation !== ''} onClick={analyze}>{operation === 'workflow' ? 'Analyzing your product…' : 'Analyze pricing ↗'}</button></article></section>
      </>}
      {productsVisible && <section className="workflow-section my-products"><div className="section-heading"><div><span className="eyebrow">YOUR WORKSPACE</span><h3>My Products</h3></div></div>{products.length ? <div className="my-product-list">{products.map(p => <article className={`my-product-row ${selected?.id === p.id ? 'active-product' : ''}`} key={p.id}><button className="product-select" disabled={operation !== ''} onClick={() => { setSelected(p); setAnalysis(null); setQuestion(''); setChatMessages([]); setConversationId(null); setChatOpen(false); }}><span className="product-dot" /><span><b>{p.product_name}</b><small>{p.brand || p.category || 'Product'} · Cost price: {currency(p.cost_price)} · Stock: {p.stock_quantity}</small></span></button><div className="action-row"><button className="outline" disabled={operation !== ''} onClick={() => editProduct(p)}>Edit</button><button className="outline" disabled={operation !== ''} onClick={() => setDeleting(p)}>Delete</button></div></article>)}</div> : <div className="empty-products"><b>No products yet.</b><span>Add your first product to get started.</span></div>}</section>}
    </main>
    <button className="chat-launch" aria-label="Open PricePulse Assistant" onClick={toggleChat}><PricePulseAssistantMark size={50} /></button>{chatOpen && <aside className="chat-panel"><div className="chat-head"><PricePulseAssistantMark size={32} /><div><b>PricePulse Assistant</b><small>{selected ? `About ${selected.product_name}` : 'Ask a business question'}</small></div><button aria-label="Close assistant" onClick={() => setChatOpen(false)}>×</button></div><div className="chat-content">{chatMessages.length === 0 && <div className="assistant-greeting"><PricePulseAssistantMark size={24} /><div><b>PricePulse Assistant</b><p>Hi! I'm PricePulse Assistant.<br />I can help you with pricing, profit, competitors, inventory, promotions, market trends, and business strategy.</p></div></div>}{selected && chatMessages.length === 0 && <div className="chat-suggestions">{['Why this price?', 'What are the product details?', 'What is my minimum margin?'].map(q => <button key={q} onClick={() => setQuestion(q)}>{q}</button>)}</div>}{chatMessages.map((message, index) => <div className={`chat-bubble ${message.role === 'assistant' ? 'answer' : ''}`} key={message.id || `${index}-${message.role}`}>{message.role === 'assistant' && <span className="chat-message-brand"><PricePulseAssistantMark size={20} /><b>PricePulse Assistant</b></span>}{message.role === 'user' && <b>You<br /></b>}{message.content}</div>)}{operation === 'chat' && <div className="chat-bubble">Thinking…</div>}</div><form className="chat-form" onSubmit={ask}><input value={question} onChange={e => setQuestion(e.target.value)} placeholder="Ask a question…" /><button disabled={operation !== '' || !conversationId || !question.trim()}>Send</button></form></aside>}
    {formOpen && <div className="modal-shade" onClick={() => operation !== 'save' && (setFormOpen(false), setEditing(null))}><form className="product-modal" onSubmit={saveProduct} onClick={e => e.stopPropagation()}><button type="button" className="modal-close" disabled={operation === 'save'} onClick={() => { setFormOpen(false); setEditing(null); }}>×</button><span className="eyebrow">PRODUCT WORKSPACE</span><h2>{editing ? 'Edit product' : 'Add a product'}</h2><p className="muted">Product and pricing details help us provide a useful recommendation.</p>{error && <div className="error-msg" role="alert">{error}</div>}{operation === 'save' && <div className="form-progress" role="status"><span className="progress-spinner" />{editing ? 'Saving your product changes…' : 'Adding your product…'}<small>Checking your product details and saving them to your workspace.</small></div>}<div className="form-grid"><label className="full">Product name<input name="product_name" required defaultValue={editing?.product_name || ''} placeholder="e.g. Botanical hair oil" /></label><label>Category<input name="category" defaultValue={editing?.category || ''} placeholder="Hair care" /></label><label>Brand<input name="brand" defaultValue={editing?.brand || ''} placeholder="Your brand" /></label><label className="full">Product details<textarea name="details" rows="3" defaultValue={editing?.product_details || ''} placeholder="Ingredients, use, quality and positioning" /></label><label>Product quantity<input name="quantity_value" type="number" min="0.001" step="any" defaultValue={editing?.quantity_value ?? ''} placeholder="e.g. 100" /></label><label>Unit<select name="quantity_unit" defaultValue={editing?.quantity_unit || 'g'}><option value="g">g</option><option value="kg">kg</option><option value="mg">mg</option><option value="ml">ml</option><option value="L">L</option></select></label><label>Cost price (₹)<input name="cost" type="number" min="0.01" step="0.01" required defaultValue={editing?.cost_price ?? ''} /></label><label>Stock quantity (packs)<input name="stock" type="number" min="0" step="1" required defaultValue={editing?.stock_quantity ?? ''} /></label><label>Minimum profit margin (%)<input name="margin" type="number" min="0" step="0.1" required defaultValue={editing?.minimum_profit_margin ?? ''} /></label></div><button className="primary wide" disabled={operation === 'save'}>{operation === 'save' ? editing ? 'Saving Changes…' : 'Adding Product…' : editing ? 'Save Changes' : 'Add Product'} <span>↗</span></button></form></div>}
    {deleting && <div className="modal-shade" role="presentation"><section className="confirm-modal" role="alertdialog" aria-modal="true" aria-labelledby="delete-title"><span className="eyebrow">REMOVE FROM YOUR WORKSPACE</span><h2 id="delete-title">Delete product?</h2><p>This will remove this product from your product list. Your shared product information will remain available for future use.</p><div className="action-row"><button className="outline" disabled={operation === 'delete'} onClick={() => setDeleting(null)}>Cancel</button><button className="danger-button" disabled={operation === 'delete'} onClick={removeProduct}>{operation === 'delete' ? 'Deleting…' : 'Delete'}</button></div></section></div>}
  </div>;
}

createRoot(document.getElementById('root')).render(<App />);
