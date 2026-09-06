import { useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import {
  Activity, AlertTriangle, ArrowRight, Bot,
  Check, CheckCircle2, ChevronDown, ChevronRight, Clipboard, Clock3, Copy, Database, FileText, Filter,
  GitBranch, LayoutDashboard, Library, Maximize2, MessageSquare,
  Link2, Play, Plug, Plus, RotateCcw, Search, Send, Settings2, ShieldCheck, SlidersHorizontal,
  Table2, Terminal, Workflow, X, Zap,
} from 'lucide-react'
import { siClaude, siCursor, siOpenai } from 'simple-icons'

type View = 'assistant' | 'pipelines' | 'mcp'
type NodeKind = 'source' | 'raw' | 'staging' | 'mart' | 'tool' | 'agent'
const API_BASE = 'http://127.0.0.1:8010'

type PipelineNode = {
  id: string
  title: string
  subtitle: string
  kind: NodeKind
  x: number
  y: number
  status?: 'fresh' | 'warning'
  meta: string
  details: string
  columns?: string[]
}

type Citation = {
  citation_id: string
  document_id: string
  document_version_id: string
  title: string
  source_url: string
  excerpt: string
  page_number?: number
  retrieval_score: number
  source_type: 'public_source' | 'synthetic_distributor_data'
  mime_type?: string
}

type AnalyticsEvidence = {
  query_id: string
  columns: string[]
  rows: Record<string, unknown>[]
  compiled_sql: string
  semantic_terms: string[]
  data_freshness?: string
  source_label: 'synthetic_distributor_data'
}

type RetrievalTrace = {
  lexical_candidates: number
  vector_candidates: number
  embedding_model: string
}

type ChatResult = {
  answer_markdown: string
  citations: Citation[]
  analytics_evidence?: AnalyticsEvidence | null
  retrieval_trace?: RetrievalTrace | null
  warnings: string[]
  tool_trace: string[]
  mode: 'model' | 'development_fallback'
}

type EvaluationResult = {
  rag: { metrics: { recall_at_1: number; recall_at_5: number; recall_at_8: number; mrr: number; citation_page_accuracy: number; no_answer_correctness: number }; cases: { passed: boolean }[] }
  analytics: { metrics: { semantic_parse_accuracy: number; unsafe_sql_rejections: number; fixture_result_correctness: number }; cases: { passed: boolean }[] }
  evaluated_at: string
}

type McpTool = { name: string; access: string; scope: string; description: string }
type McpClientKey = 'codex' | 'claude' | 'cursor'
type McpClientConfig = { name: string; filename: string; instructions: string[]; configuration: string | Record<string, unknown> }
type McpStatus = {
  running: boolean
  access: string
  transports: string[]
  http_endpoint: string
  tool_count: number
  tools: McpTool[]
  client_configs: Record<McpClientKey, McpClientConfig>
  client_last_used: Partial<Record<McpClientKey, string>>
}
type McpCall = {
  request_id: string
  client_name: string
  tool_name: string
  status: 'success' | 'no_result' | 'clarification_required' | 'refused' | 'error'
  result_summary: string
  result_count: number
  duration_ms: number
  error_code?: string | null
  created_at: string
}

const initialNodes: PipelineNode[] = [
  { id: 'nhtsa', title: 'NHTSA Recalls API', subtitle: 'SOURCE · daily JSON', kind: 'source', x: 42, y: 170, meta: '1 endpoint · 12,884 records', details: 'Pulls the public NHTSA recalls feed each morning. The loader records the source timestamp and campaign ID before landing data.', status: 'fresh' },
  { id: 'raw', title: 'raw_recalls', subtitle: 'SNOWFLAKE TABLE', kind: 'raw', x: 200, y: 108, meta: '12,884 rows · append', details: 'Immutable landing table. Keeps the original payload so model changes can be replayed without re-downloading the source.', status: 'fresh', columns: ['campaign_number', 'manufacturer', 'component', 'report_date'] },
  { id: 'stg', title: 'stg_recalls', subtitle: 'DBT MODEL · clean', kind: 'staging', x: 370, y: 108, meta: '12,884 rows · 8 tests', details: 'Normalizes dates, manufacturer names, and boolean advisories. Duplicate campaigns are rejected at this boundary.', status: 'fresh', columns: ['campaign_id', 'make', 'model', 'do_not_drive'] },
  { id: 'mart', title: 'mart_recall_overview', subtitle: 'DBT MART · fresh', kind: 'mart', x: 540, y: 160, meta: '12,884 rows · 6 tests', details: 'One row per recall campaign. This is the governed analytical surface used by the text-to-SQL tool.', status: 'fresh', columns: ['campaign_id', 'manufacturer', 'affected_units', 'remedy_rate'] },
  { id: 'search', title: 'search_recall_documents', subtitle: 'MCP TOOL · read-only', kind: 'tool', x: 540, y: 330, meta: 'policy + recall library', details: 'Hybrid search over recall narratives and NHTSA guidance. Returns excerpts with document IDs and page metadata.', status: 'fresh' },
  { id: 'sql', title: 'query_recall_analytics', subtitle: 'MCP TOOL · governed', kind: 'tool', x: 700, y: 112, meta: 'read-only · semantic layer', details: 'Maps plain-language questions to approved metrics and executes read-only SQL against the mart.', status: 'fresh' },
  { id: 'agent', title: 'Recall RAG assistant', subtitle: 'AI AGENT · 2 tools', kind: 'agent', x: 700, y: 286, meta: 'chat + citations', details: 'Routes narrative questions to retrieval and numeric questions to the analytics tool, then combines both with source labels.', status: 'fresh' },
]

const edges: [string, string][] = [['nhtsa', 'raw'], ['raw', 'stg'], ['stg', 'demand'], ['demand', 'risk'], ['risk', 'transfer'], ['risk', 'analytics'], ['search', 'agent'], ['analytics', 'agent']]

type Conversation = { id: string; title: string; time: string }
type ConversationState = { sentQuestion: string; chatResult: ChatResult | null }

const initialConversations: Conversation[] = [
  { id: 'brake-system', title: 'Brake-system recalls', time: '2m' },
  { id: 'advisories', title: 'Do-not-drive advisories', time: '18m' },
  { id: 'manufacturer-trends', title: 'Manufacturer trend check', time: 'Yesterday' },
  { id: 'remedy-rate', title: 'Remedy-rate baseline', time: 'Mon' },
]

const citedDocs: Citation[] = [
  { citation_id: 'demo-24v118', document_id: 'demo-24v118', document_version_id: 'demo-v1', title: 'Recall 24V-118 · Brake hose', source_url: 'https://www.nhtsa.gov/recalls', excerpt: 'The brake hose may rupture, causing a loss of brake fluid and reduced braking performance.', page_number: 1, retrieval_score: 1, source_type: 'public_source' },
  { citation_id: 'demo-guidance', document_id: 'demo-guidance', document_version_id: 'demo-v1', title: 'MVS Defects and Recalls', source_url: 'https://www.nhtsa.gov/recalls', excerpt: 'Manufacturers must notify owners and provide a remedy without charge.', page_number: 7, retrieval_score: 1, source_type: 'public_source', mime_type: 'application/pdf' },
  { citation_id: 'demo-23v441', document_id: 'demo-23v441', document_version_id: 'demo-v1', title: 'Recall 23V-441 · Hydraulic line', source_url: 'https://www.nhtsa.gov/recalls', excerpt: 'Vehicles with the condition should be parked outside and away from structures.', page_number: 2, retrieval_score: 1, source_type: 'public_source' },
]

function App() {
  const [view, setView] = useState<View>(() => {
    const requested = new URLSearchParams(window.location.search).get('view')
    return requested === 'pipelines' || requested === 'mcp' ? requested : 'assistant'
  })
  const [selectedDoc, setSelectedDoc] = useState(citedDocs[0])
  const [question, setQuestion] = useState('')
  const [conversations, setConversations] = useState<Conversation[]>(initialConversations)
  const [activeConversationId, setActiveConversationId] = useState(initialConversations[0].id)
  const [conversationState, setConversationState] = useState<Record<string, ConversationState>>({})
  const [nodes, setNodes] = useState(initialNodes)
  const [selectedNode, setSelectedNode] = useState<PipelineNode | null>(initialNodes[3])
  const [runCount, setRunCount] = useState(4)
  const [toast, setToast] = useState('')
  const [apiStatus, setApiStatus] = useState<'offline' | 'online'>('offline')
  const [liveRecords, setLiveRecords] = useState(0)
  const [modelMode, setModelMode] = useState<'model' | 'development_fallback'>('development_fallback')
  const [evaluationResult, setEvaluationResult] = useState<EvaluationResult | null>(null)
  const activeConversation = conversations.find((conversation) => conversation.id === activeConversationId) ?? conversations[0]
  const activeState = conversationState[activeConversationId] ?? { sentQuestion: '', chatResult: null }

  useEffect(() => {
    fetch(`${API_BASE}/api/health`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('offline')))
      .then((payload) => { setApiStatus('online'); setLiveRecords(payload.status?.sources?.nhtsa_recalls?.record_count ?? 0); setModelMode(payload.status?.model_mode ?? 'development_fallback') })
      .catch(() => setApiStatus('offline'))
    fetch(`${API_BASE}/api/evaluations`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('offline')))
      .then((payload) => setEvaluationResult(payload as EvaluationResult))
      .catch(() => setEvaluationResult(null))
    fetch(`${API_BASE}/api/pipelines`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('offline')))
      .then((payload) => {
        const positions: Record<string, [number, number]> = { nhtsa: [42, 170], raw: [200, 108], stg: [370, 108], demand: [540, 108], risk: [690, 108], transfer: [690, 270], search: [540, 330], analytics: [850, 112], agent: [850, 286] }
        const mapped = (payload.nodes as { id: string; title: string; kind: NodeKind; status: 'fresh' | 'warning'; meta: string }[]).map((node) => ({ ...node, subtitle: node.kind === 'mart' ? 'DBT MART · fresh' : node.kind === 'tool' ? 'MCP TOOL · read-only' : node.kind === 'staging' ? 'DBT MODEL · clean' : node.kind === 'raw' ? 'POSTGRES · raw' : node.kind === 'agent' ? 'AI ASSISTANT · routed' : 'SOURCE · public', x: positions[node.id]?.[0] ?? 40, y: positions[node.id]?.[1] ?? 40, details: node.meta, columns: [] }))
        if (mapped.length) { setNodes(mapped); setSelectedNode(mapped.find((node) => node.id === 'risk') ?? mapped[0]) }
      })
      .catch(() => undefined)
  }, [])

  const showToast = (message: string) => {
    setToast(message)
    window.setTimeout(() => setToast(''), 2600)
  }

  const submitQuestion = async () => {
    if (!question.trim()) return
    const nextQuestion = question.trim()
    const conversationId = activeConversationId
    setConversationState((state) => ({ ...state, [conversationId]: { sentQuestion: nextQuestion, chatResult: null } }))
    setQuestion('')
    try {
      const response = await fetch(`${API_BASE}/api/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: nextQuestion }) })
      if (!response.ok) throw new Error('offline')
      const payload = await response.json() as ChatResult
      setApiStatus('online')
      setConversationState((state) => ({ ...state, [conversationId]: { sentQuestion: nextQuestion, chatResult: payload } }))
      if (payload.citations?.length) setSelectedDoc(payload.citations[0])
      showToast('Grounded answer loaded from the recall index')
    } catch {
      showToast('Could not reach the local API. Start it to run this investigation.')
    }
  }

  const selectConversation = (conversation: Conversation) => {
    setActiveConversationId(conversation.id)
    setQuestion('')
    setView('assistant')
  }

  const createInvestigation = () => {
    const id = `investigation-${Date.now()}`
    const investigation = { id, title: 'New Chat', time: 'now' }
    setConversations((items) => [investigation, ...items])
    setConversationState((state) => ({ ...state, [id]: { sentQuestion: '', chatResult: null } }))
    setActiveConversationId(id)
    setQuestion('')
    setView('assistant')
    showToast('New chat started')
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark"><span className="brand-orbit" /><span>Recall <span>RAG</span></span></div>
        <div className="topbar-context"><span className="context-dot" />NHTSA recall intelligence <span className="slash">/</span> {apiStatus === 'online' ? `${modelMode === 'model' ? 'model-backed' : 'development fallback'} · API connected` : 'local demo mode'}</div>
        <div className="topbar-actions"><button className="avatar">SH</button></div>
      </header>

      <div className="workspace">
        <aside className="sidebar">
          <div className="sidebar-heading"><div className="workspace-icon"><ShieldCheck size={16} /></div><div><strong>Recall RAG</strong><small>Operations workspace</small></div><ChevronDown size={15} /></div>
          <div className="sidebar-top"><button className="new-button" onClick={createInvestigation}><Plus size={16} /> New Chat</button>
          <label className="search-field"><Search size={15} /><input placeholder="Search investigations" /></label>
          <div className="sidebar-label">PINNED</div>
          <div className="conversation-list">{conversations.map((item) => <button key={item.id} className={`conversation ${item.id === activeConversationId && view === 'assistant' ? 'active' : ''}`} onClick={() => selectConversation(item)}><span className="conversation-icon">◆</span><span className="conversation-copy"><span>{item.title}</span><small>{item.time}</small></span></button>)}</div></div>
          <div className="sidebar-bottom"><nav className="primary-nav">
            <button className={view === 'assistant' ? 'selected' : ''} onClick={() => setView('assistant')}><MessageSquare size={15} /> Assistant</button>
            <button className={view === 'pipelines' ? 'selected' : ''} onClick={() => setView('pipelines')}><Workflow size={15} /> Workflows &amp; pipelines</button>
            <button className={view === 'mcp' ? 'selected' : ''} onClick={() => setView('mcp')}><Plug size={15} /> MCP Access</button>
          </nav>
          <div className="profile-row"><span className="status-ring" /><span>M. Ortega · Ops</span><Settings2 size={14} /></div></div>
        </aside>

        {view === 'assistant'
          ? <AssistantView {...{ selectedDoc, setSelectedDoc, question, setQuestion, sentQuestion: activeState.sentQuestion, submitQuestion, liveRecords, chatResult: activeState.chatResult, conversationTitle: activeConversation.title }} />
          : view === 'pipelines'
            ? <PipelineView {...{ nodes, setNodes, selectedNode, setSelectedNode, runCount, setRunCount, showToast, evaluationResult, setEvaluationResult }} />
            : <McpAccessView showToast={showToast} />}
      </div>
      {toast && <div className="toast"><Check size={15} />{toast}</div>}
    </div>
  )
}

type AssistantProps = {
  selectedDoc: typeof citedDocs[number]
  setSelectedDoc: (doc: typeof citedDocs[number]) => void
  question: string
  setQuestion: (value: string) => void
  sentQuestion: string
  submitQuestion: () => void
  liveRecords: number
  chatResult: ChatResult | null
  conversationTitle: string
}

function AssistantView({ selectedDoc, setSelectedDoc, question, setQuestion, sentQuestion, submitQuestion, liveRecords, chatResult, conversationTitle }: AssistantProps) {
  return <main className="main-view assistant-view">
    <div className="view-header"><div><div className="eyebrow"><Activity size={13} /> INVESTIGATION · DFW-01</div><h1>{conversationTitle}</h1></div></div>
    <div className="assistant-grid">
      <section className="chat-column">
        {sentQuestion ? <><div className="question-row"><span className="question-avatar">you</span><div className="question-bubble">{sentQuestion}</div></div><div className="answer-block"><div className="ai-avatar"><Bot size={17} /></div><div className="answer-content"><div className="answer-meta"><span>Recall RAG assistant</span><span className="meta-separator">·</span><span className="live-label"><span className="live-dot" /> {chatResult ? (chatResult.mode === 'model' ? 'model-backed grounded answer' : 'grounded development fallback') : 'searching connected sources'}</span></div>{chatResult ? <><div className="answer-line rich-answer">{renderAnswerMarkdown(chatResult.answer_markdown)}</div>{chatResult.warnings.map((warning) => <p className="answer-line muted" key={warning}>{warning}</p>)}<SourceList citations={chatResult.citations} onSelect={setSelectedDoc} /></> : <AnswerLoadingSkeleton />}<StarterQuestions setQuestion={setQuestion} compact /></div></div></> : <div className="starter-state"><div className="ai-avatar"><Bot size={17} /></div><div><div className="answer-meta"><span>Recall RAG assistant</span><span className="meta-separator">·</span><span className="live-label"><span className="live-dot" /> ready to answer with connected sources</span></div><h2>Ask a question</h2><p>Recall RAG answers vehicle-recall questions by searching public documents with citations and querying structured recall data. Its governed MCP tools also let external assistants use these read-only capabilities safely.</p><StarterQuestions setQuestion={setQuestion} /></div></div>}
        <div className="chat-spacer" />
        <div className="composer-wrap"><div className="composer"><textarea value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submitQuestion() } }} placeholder="Ask about recall evidence, stockout risk, or transfer candidates…" rows={1} /><div className="composer-actions"><span><Database size={13} /> NHTSA snapshot · {liveRecords.toLocaleString()} records</span><button className="send-button" onClick={submitQuestion} aria-label="Send"><Send size={15} /></button></div></div><div className="composer-note">The assistant can search public evidence and run read-only queries over synthetic distributor data.</div></div>
      </section>
      <EvidencePanel {...{ selectedDoc, setSelectedDoc, citations: chatResult?.citations, chatResult }} />
    </div>
  </main>
}

function StarterQuestions({ setQuestion, compact = false }: { setQuestion: (question: string) => void; compact?: boolean }) {
  const questions = [
    'What does NHTSA guidance say about the remedy for a safety defect?',
    'How many recall campaigns were reported by model year?',
    'Which critical recalls affect Dallas stockout risk, and what safety consequence does NHTSA report?',
  ]
  return <div className={`suggested-row ${compact ? 'compact' : ''}`}>{questions.map((question) => <button key={question} onClick={() => setQuestion(question)}>{question} <ArrowRight size={13} /></button>)}</div>
}

function AnswerLoadingSkeleton() {
  return <div className="answer-skeleton" role="status" aria-label="Preparing grounded answer">
    <span className="answer-skeleton-line answer-skeleton-line--long" />
    <span className="answer-skeleton-line answer-skeleton-line--wide" />
    <span className="answer-skeleton-line answer-skeleton-line--medium" />
    <span className="answer-skeleton-line answer-skeleton-line--short" />
  </div>
}

function renderAnswerMarkdown(markdown: string) {
  const cleaned = markdown
    .replace(/\s*\[source:\s*\[?chunk_[^\]]+\]?\]\s*/gi, ' ')
    .replace(/\s*\[chunk_[^\]]+\]\s*/gi, ' ')
    .replace(/\s*\[(?:citation_id|citation id):\s*chunk_[^\]]+\]\s*/gi, ' ')
    .trim()
  return cleaned.split(/\n{2,}/).map((paragraph, index) => <span key={index}>{index > 0 && <><br /><br /></>}{renderAnswerInline(paragraph)}</span>)
}

function renderAnswerInline(text: string) {
  // The prompt asks for Markdown emphasis, while this fallback also keeps
  // older/model-noncompliant responses readable and scannable.
  const emphasis = /(\b(?:NHTSA|Ford|Honda|Toyota|recall(?:s)?|safety defect(?:s)?|remed(?:y|ies)|vehicle owners?|no cost|\d[\d,.]*(?:%|\s*(?:units?|vehicles?|campaigns?|rows?))?)\b)/i
  const citation = /(\(?\s*(?:citation_id|citation id)\s*:\s*chunk_[a-z0-9-]+\s*\)?)/gi
  const renderEmphasis = (value: string, keyPrefix: string) => value.split(/(\*\*[^*]+\*\*)/g).flatMap((part, partIndex) => {
    if (part.startsWith('**') && part.endsWith('**')) return [<strong key={`${keyPrefix}-bold-${partIndex}`}>{part.slice(2, -2)}</strong>]
    return part.split(emphasis).map((segment, segmentIndex) => emphasis.test(segment) ? <strong key={`${keyPrefix}-${partIndex}-${segmentIndex}`}>{segment}</strong> : <span key={`${keyPrefix}-${partIndex}-${segmentIndex}`}>{segment}</span>)
  })
  return text.split(citation).flatMap((part, partIndex) => partIndex % 2 === 1
    ? [<span className="citation-chunk" key={`citation-${partIndex}`}>{part}</span>]
    : renderEmphasis(part, `plain-${partIndex}`))
}

function SourceList({ citations, onSelect }: { citations: Citation[]; onSelect: (citation: Citation) => void }) {
  if (!citations.length) return null
  return <div className="source-list"><div className="source-list-heading"><Link2 size={14} /><span>Sources</span><small>{citations.length}</small></div>{citations.map((citation, index) => <button key={citation.citation_id} className="source-row" onClick={() => onSelect(citation)}><Link2 size={14} /><span><strong>{citation.title}</strong><small>{citation.page_number ? `Page ${citation.page_number}` : 'Connected source'}</small></span><span className="source-row-number">{index + 1}</span></button>)}</div>
}

type EvidenceProps = Pick<AssistantProps, 'selectedDoc' | 'setSelectedDoc'> & { citations?: Citation[]; chatResult?: ChatResult | null }

function EvidencePanel({ selectedDoc, setSelectedDoc, citations, chatResult }: EvidenceProps) {
  // Evidence is intentionally empty until an investigation has run. This keeps
  // the opening state honest: starter questions are prompts, not a preloaded answer.
  const docs = citations ?? []
  const isPdf = selectedDoc.mime_type === 'application/pdf'
  const pdfUrl = `${API_BASE}/api/documents/${selectedDoc.document_version_id}/pages/${selectedDoc.page_number ?? 1}/image?citation_id=${encodeURIComponent(selectedDoc.citation_id)}`
  return <aside className="evidence-panel"><div className="evidence-header"><div><span className="eyebrow">EVIDENCE</span></div><button className="icon-button" aria-label="Collapse evidence"><ChevronRight size={16} /></button></div><section className="evidence-section"><div className="evidence-section-heading"><span>DOCUMENTS</span><small>{docs.length ? `${docs.length} source${docs.length === 1 ? '' : 's'} cited` : 'No sources used yet'}</small></div>{docs.length ? <><div className="doc-list">{docs.map((doc, index) => <button key={doc.citation_id} className={`doc-card ${selectedDoc.citation_id === doc.citation_id ? 'selected' : ''}`} onClick={() => setSelectedDoc(doc)}><span className="doc-number">{index + 1}</span><span className="doc-card-copy"><strong>{doc.title}</strong><small>Public NHTSA source · p. {doc.page_number ?? '—'}</small></span><ChevronRight size={14} /></button>)}</div><div className="document-viewer"><div className="viewer-toolbar"><span><FileText size={13} /> {isPdf ? 'Official PDF' : 'API record'} · page {selectedDoc.page_number ?? '—'}</span><a href={isPdf ? pdfUrl : selectedDoc.source_url} target="_blank" rel="noreferrer" aria-label="Open source"><Maximize2 size={13} /></a></div>{isPdf ? <iframe className="pdf-frame" title={`${selectedDoc.title} page ${selectedDoc.page_number ?? 1}`} src={pdfUrl} /> : <div className="paper"><div className="paper-title">{selectedDoc.title}</div><div className="paper-rule" /><p>Public safety evidence</p><div className="highlighted">{selectedDoc.excerpt}</div><p className="paper-copy muted">Source: <a href={selectedDoc.source_url} target="_blank" rel="noreferrer">National Highway Traffic Safety Administration</a><br />Citation ID {selectedDoc.citation_id}</p></div>}<div className="citation-excerpt"><strong>Cited passage</strong><span>{selectedDoc.excerpt}</span></div></div></> : <div className="empty-evidence"><FileText size={19} /><strong>No document evidence used</strong><span>{chatResult?.analytics_evidence ? 'This answer came from the governed analytics mart, not the document index.' : 'Ask an investigation question to cite a source from the system.'}</span></div>}<RetrievalTracePanel chatResult={chatResult} /></section><section className="evidence-section"><div className="evidence-section-heading"><span>DATA USED</span><small>{chatResult?.analytics_evidence ? 'Governed query executed' : 'No analytics query'}</small></div><DataUsed chatResult={chatResult} /></section></aside>
}

function RetrievalTracePanel({ chatResult }: { chatResult?: ChatResult | null }) {
  if (!chatResult) return null
  const trace = chatResult.retrieval_trace
  const sources = chatResult.citations.length
  return <div className="retrieval-trace"><div className="retrieval-trace-heading"><span>RETRIEVAL TRACE</span><small>{trace ? 'Completed' : 'Not run'}</small></div><div className="retrieval-methods"><div><span>Keyword search</span><strong>{trace ? `${trace.lexical_candidates} candidates` : 'Not run'}</strong></div><div><span>Vector search</span><strong>{trace ? `${trace.vector_candidates} candidates` : 'Not run'}</strong></div></div><div className="grounding-meter"><div className="grounding-meter-label"><span>GROUNDING</span><strong>{sources ? 'Citations attached' : 'No citations'}</strong></div><div className="grounding-track"><span style={{ width: sources ? '100%' : '0%' }} /></div><div className="grounding-stats"><span><strong>{sources}</strong> public source{sources === 1 ? '' : 's'}</span><span><strong>{sources ? 'Yes' : 'No'}</strong> claims with citations</span></div></div>{trace && <small className="embedding-note">{trace.embedding_model}</small>}</div>
}

function DataUsed({ chatResult }: { chatResult?: ChatResult | null }) { const analytics = chatResult?.analytics_evidence; return <div className="data-used"><div className="data-card"><span className="data-icon"><Database size={15} /></span><div><strong>{analytics ? 'dbt analytics mart' : 'No analytics mart used'}</strong><small>{analytics ? `synthetic distributor data · ${analytics.rows.length} rows` : 'Ask a numerical question to run a governed query.'}</small></div>{analytics && <Check size={14} className="success-icon" />}</div><div className="data-card"><span className="data-icon violet"><Library size={15} /></span><div><strong>Document evidence index</strong><small>PostgreSQL FTS + pgvector · reciprocal-rank fusion</small></div><Check size={14} className="success-icon" /></div><div className="text-to-sql-heading">Text to SQL</div><div className="sql-preview"><div><Terminal size={13} /> {analytics ? 'compiled SQL · semantic layer mapped' : 'governed analytics'}</div><code>{analytics?.compiled_sql ?? 'No governed query was needed for this answer.'}</code></div>{analytics && <div className="semantic-terms"><span>BUSINESS TERMS</span>{analytics.semantic_terms.map((term) => <small key={term}>{term}</small>)}</div>}<div className="governance-note"><ShieldCheck size={14} /><span>Read-only query. The model supplied an intent; the backend compiled and validated SQL.</span></div></div> }

const MCP_CLIENTS: McpClientKey[] = ['codex', 'claude', 'cursor']

function AgentLogo({ client }: { client: McpClientKey }) {
  const icon = client === 'codex' ? siOpenai : client === 'claude' ? siClaude : siCursor
  return <span className={`agent-logo agent-logo--${client}`} aria-hidden="true"><svg viewBox="0 0 24 24"><path d={icon.path} /></svg></span>
}

function McpAccessView({ showToast }: { showToast: (message: string) => void }) {
  const [status, setStatus] = useState<McpStatus | null>(null)
  const [calls, setCalls] = useState<McpCall[]>([])
  const [selectedClient, setSelectedClient] = useState<McpClientKey>('claude')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const [statusResponse, callsResponse] = await Promise.all([
          fetch(`${API_BASE}/api/mcp/status`),
          fetch(`${API_BASE}/api/mcp/calls?limit=20`),
        ])
        if (!statusResponse.ok || !callsResponse.ok) throw new Error('MCP API unavailable')
        const [statusPayload, callsPayload] = await Promise.all([statusResponse.json(), callsResponse.json()])
        if (active) {
          setStatus(statusPayload as McpStatus)
          setCalls((callsPayload as { calls: McpCall[] }).calls)
        }
      } catch {
        if (active) setStatus(null)
      } finally {
        if (active) setLoading(false)
      }
    }
    void load()
    const interval = window.setInterval(load, 5000)
    return () => { active = false; window.clearInterval(interval) }
  }, [])

  const client = status?.client_configs[selectedClient]
  const configuration = client
    ? typeof client.configuration === 'string'
      ? client.configuration
      : JSON.stringify(client.configuration, null, 2)
    : ''

  const copyConfiguration = async () => {
    if (!configuration) return
    try {
      await navigator.clipboard.writeText(configuration)
      showToast(`${client?.name ?? 'Agent'} configuration copied`)
    } catch {
      showToast('Could not copy automatically · select the configuration text')
    }
  }

  return <main className="main-view mcp-view">
    <header className="mcp-header">
      <div>
        <div className="mcp-title-line"><h1>MCP Access</h1><span className={`mcp-running ${status?.running ? 'online' : ''}`}><span />{loading ? 'Checking' : status?.running ? 'Running' : 'Offline'}</span><span className="mcp-readonly">READ-ONLY</span></div>
        <p>Connect external AI agents to Recall RAG through governed, read-only tools.</p>
      </div>
      <div className="mcp-transport"><span>Transport</span><strong>{status?.transports.includes('stdio') ? 'stdio' : '—'} / {status?.transports.includes('streamable-http') ? 'local HTTP' : '—'}</strong></div>
    </header>

    <div className="mcp-content">
      <section className="mcp-section">
        <div className="mcp-section-title"><h2>Available tools</h2><span>What a connected agent is allowed to do</span></div>
        <div className="mcp-tool-grid">
          {(status?.tools ?? []).map((tool) => <article className="mcp-tool-card" key={tool.name}>
            <code>{tool.name}</code>
            <p>{tool.description}</p>
            <div><span>{tool.access.toUpperCase()}</span><small>{tool.scope}</small></div>
          </article>)}
          {!loading && !status && <div className="mcp-empty-inline">The MCP server status is unavailable.</div>}
        </div>
      </section>

      <section className="mcp-section">
        <div className="mcp-section-title"><h2>Connect to your agent</h2></div>
        <div className="agent-setup-panel">
          <div className="agent-tabs" role="tablist" aria-label="MCP client">
            {MCP_CLIENTS.map((key) => {
              const config = status?.client_configs[key]
              const usedAt = status?.client_last_used[key]
              return <button key={key} role="tab" aria-selected={selectedClient === key} className={selectedClient === key ? 'selected' : ''} onClick={() => setSelectedClient(key)}>
                <AgentLogo client={key} />
                <span><strong>{config?.name ?? key}</strong><small className={usedAt ? 'used' : ''}><i />{usedAt ? 'Used recently' : 'Ready to configure'}</small></span>
              </button>
            })}
          </div>
          <div className="agent-setup-body">
            <div className="agent-steps">
              <h3>Set up {client?.name ?? 'your agent'}</h3>
              <ol>{(client?.instructions ?? []).map((instruction, index) => <li key={instruction}><span>{index + 1}</span><p>{instruction}</p></li>)}</ol>
            </div>
            <div className="agent-config">
              <div className="agent-config-heading"><code>{client?.filename ?? 'configuration'}</code><button onClick={copyConfiguration}><Copy size={13} /> Copy configuration</button></div>
              <pre><code>{configuration || 'Waiting for the local MCP server…'}</code></pre>
              <p>The agent receives only the three read-only tools above. No database credentials or local source paths are returned by tool calls.</p>
            </div>
          </div>
        </div>
      </section>

      <section className="mcp-section mcp-recent-section">
        <div className="mcp-section-title"><h2>Recent calls</h2><span>Live MCP activity · refreshed every 5 seconds</span></div>
        <div className="mcp-calls-table">
          <div className="mcp-call-row header"><span>TIME</span><span>CLIENT</span><span>TOOL</span><span>RESULT</span><span>DURATION</span><span>STATUS</span></div>
          {calls.map((call) => <div className="mcp-call-row" key={`${call.request_id}-${call.created_at}`}>
            <span>{new Date(call.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
            <span>{call.client_name}</span>
            <code>{call.tool_name}</code>
            <span>{call.result_summary}</span>
            <span>{(call.duration_ms / 1000).toFixed(2)}s</span>
            <span className={`mcp-call-status ${call.status}`}><i />{call.status.replace('_', ' ')}</span>
          </div>)}
          {!calls.length && <div className="mcp-calls-empty"><Clock3 size={17} /><span><strong>No external MCP calls yet</strong>Connect an agent above and ask it to use a Recall RAG tool.</span></div>}
        </div>
      </section>
    </div>
  </main>
}

type PipelineProps = { nodes: PipelineNode[]; setNodes: React.Dispatch<React.SetStateAction<PipelineNode[]>>; selectedNode: PipelineNode | null; setSelectedNode: (node: PipelineNode | null) => void; runCount: number; setRunCount: React.Dispatch<React.SetStateAction<number>>; showToast: (message: string) => void; evaluationResult: EvaluationResult | null; setEvaluationResult: (result: EvaluationResult) => void }

function PipelineView({ nodes, setNodes, selectedNode, setSelectedNode, runCount, setRunCount, showToast, evaluationResult, setEvaluationResult }: PipelineProps) {
  const canvasRef = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<{ id: string; dx: number; dy: number } | null>(null)
  const [search, setSearch] = useState('')
  const [pipeline, setPipeline] = useState('Recall intelligence')
  const filteredNodes = useMemo(() => nodes.filter((node) => `${node.title} ${node.subtitle}`.toLowerCase().includes(search.toLowerCase())), [nodes, search])
  const nodeMap = new Map(nodes.map((node) => [node.id, node]))

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>, node: PipelineNode) => {
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    setSelectedNode(node)
    setDrag({ id: node.id, dx: event.clientX - rect.left - node.x, dy: event.clientY - rect.top - node.y })
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!drag) return
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    const x = Math.max(18, event.clientX - rect.left - drag.dx)
    const y = Math.max(18, event.clientY - rect.top - drag.dy)
    setNodes((current) => current.map((node) => node.id === drag.id ? { ...node, x, y } : node))
  }
  const onPointerUp = () => setDrag(null)
  const resetLayout = () => { setNodes(initialNodes); setSelectedNode(initialNodes[3]); showToast('Layout reset') }
  const runPipeline = async () => {
    setRunCount((count) => count + 1)
    try {
      const response = await fetch(`${API_BASE}/api/refresh`, { method: 'POST' })
      if (!response.ok) throw new Error('offline')
      const evaluationResponse = await fetch(`${API_BASE}/api/evaluations`)
      if (evaluationResponse.ok) setEvaluationResult(await evaluationResponse.json() as EvaluationResult)
      showToast('NHTSA refresh, dbt build, and RAG reindex completed')
    } catch {
      showToast('Refresh failed · check the local API and data-source status')
    }
  }

  return <main className="main-view pipeline-view"><div className="pipeline-toolbar"><div><div className="eyebrow"><Workflow size={13} /> DATA &amp; AI OPERATIONS</div><h1>Workflow canvas</h1></div><div className="pipeline-actions"><label className="pipeline-select"><span>PIPELINE</span><select value={pipeline} onChange={(event) => setPipeline(event.target.value)}><option>Recall intelligence</option><option>Daily source refresh</option></select><ChevronDown size={13} /></label><span className="refresh-note"><span className="live-dot" /> Last refreshed today 14:11</span><label className="pipeline-search"><Search size={14} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search nodes" /></label><button className="secondary-button" onClick={resetLayout}><RotateCcw size={14} /> Reset layout</button><button className="primary-button" onClick={runPipeline}><Play size={13} /> Load latest snapshot</button></div></div><div className="pipeline-workspace"><div className="canvas-area"><div className="canvas-legend"><span><i className="legend-dot source" /> source</span><span><i className="legend-dot transform" /> transform</span><span><i className="legend-dot governed" /> governed tool</span></div><div className="canvas" ref={canvasRef} onPointerMove={onPointerMove} onPointerUp={onPointerUp}>{<svg className="edges" aria-hidden="true">{edges.map(([from, to]) => { const source = nodeMap.get(from); const target = nodeMap.get(to); if (!source || !target) return null; return <path key={`${from}-${to}`} d={`M ${source.x + 160} ${source.y + 42} C ${source.x + 195} ${source.y + 42}, ${target.x - 35} ${target.y + 42}, ${target.x} ${target.y + 42}`} /> })}</svg>}{filteredNodes.map((node) => <div key={node.id} className={`pipeline-node ${node.kind} ${selectedNode?.id === node.id ? 'selected' : ''}`} style={{ left: node.x, top: node.y }} onPointerDown={(event) => onPointerDown(event, node)}><div className="node-top"><span className="node-kind">{node.subtitle}</span>{node.status === 'warning' ? <AlertTriangle size={12} className="warning-icon" /> : <span className="fresh-dot" />}</div><strong>{node.title}</strong><small>{node.meta}</small><div className="node-ports"><span /><span /></div></div>)}</div><div className="canvas-footer"><span><SlidersHorizontal size={13} /> Drag to rearrange · select a node for lineage</span><span>Source of truth: dbt model contract <GitBranch size={13} /></span></div></div><NodeInspector node={selectedNode} onClose={() => setSelectedNode(null)} onRun={runPipeline} /></div><PipelineRuns runCount={runCount} evaluationResult={evaluationResult} /></main>
}

function NodeInspector({ node, onClose, onRun }: { node: PipelineNode | null; onClose: () => void; onRun: () => void }) { if (!node) return <aside className="node-inspector empty"><div className="empty-inspector"><GitBranch size={22} /><strong>Select a node</strong><span>Inspect lineage, tests, and model metadata.</span></div></aside>; return <aside className="node-inspector"><div className="inspector-header"><div><span className="eyebrow">{node.kind === 'mart' ? 'DBT MART' : node.kind === 'tool' ? 'MCP TOOL' : node.kind.toUpperCase()}</span><h2>{node.title}</h2></div><button className="icon-button" onClick={onClose}><X size={15} /></button></div><div className="inspector-tabs"><button className="active">Overview</button><button>Columns</button><button>Lineage</button></div><div className="inspector-body"><p>{node.details}</p><div className="inspector-grid"><span>Type</span><strong>{node.kind === 'mart' ? 'dbt model' : node.kind === 'tool' ? 'MCP read-only' : node.kind}</strong><span>Materialization</span><strong>{node.kind === 'mart' ? 'table · incremental' : 'managed step'}</strong><span>Last run</span><strong>Today 14:11</strong><span>Tests</span><strong className="test-pass"><Check size={12} /> {node.kind === 'mart' ? '6 passed' : '8 passed'}</strong></div>{node.columns && <div className="column-list"><div className="section-label">COLUMNS</div>{node.columns.map((column) => <div key={column}><Table2 size={13} />{column}<span>string</span></div>)}</div>}<div className="inspector-callout"><ShieldCheck size={14} /><span>Used by <strong>Recall RAG assistant</strong>. Governed definitions come from the semantic layer.</span></div><button className="inspector-run" onClick={onRun}><Play size={13} /> Run this node</button></div></aside> }

function PipelineRuns({ runCount, evaluationResult }: { runCount: number; evaluationResult: EvaluationResult | null }) {
  const rag = evaluationResult?.rag.metrics
  const analytics = evaluationResult?.analytics.metrics
  const score = rag ? `R@5 ${(rag.recall_at_5 * 100).toFixed(0)}% · MRR ${rag.mrr.toFixed(2)} · no-answer ${(rag.no_answer_correctness * 100).toFixed(0)}%` : 'Run the API to calculate retrieval quality'
  return <div className="pipeline-runs"><div className="runs-heading"><span><ChevronDown size={14} /> Recent pipeline runs</span><small>{runCount} local runs</small></div><div className="runs-table"><div className="runs-row header"><span>RUN</span><span>TRIGGER</span><span>STARTED</span><span>DURATION</span><span>RESULT</span></div><div className="runs-row"><span><strong>run_{2840 + runCount}</strong></span><span>Refresh + dbt build</span><span>Current session</span><span>local</span><span className="result"><Check size={12} /> dbt + evals</span></div></div><div className="collapsed-row"><ChevronRight size={13} /> Semantic definitions <span>4 governed marts</span></div><div className="collapsed-row"><ChevronRight size={13} /> AI evaluations <span>{score}{analytics ? ` · SQL ${Math.round(analytics.semantic_parse_accuracy * 100)}%` : ''}</span></div></div>
}

export default App
