# Chief of Staff Application

> **An AI-powered executive assistant that turns an overloaded inbox into a prioritized action queue — while keeping the human in control of consequential actions.**

---

## 🚀 Product Overview

**Chief of Staff Application** is a working AI product prototype designed to help professionals and individual to manage their email overload and reduce the cognitive effort required to process communication.

Instead of simply summarizing emails, the product helps answer:

- What needs my attention?
- What requires a response?
- What should I do next?
- Can AI prepare the response for me?
- Does this require meeting coordination?

The product combines:

- **AI email triage**
- **Context-aware classification**
- **AI draft generation**
- **Human approval**
- **Gmail integration**
- **Calendar coordination**

The project is designed from an **AI Product Management perspective**, combining product strategy, AI workflow design, human-in-the-loop controls, evaluation, cost considerations, and technical implementation.

---

# 🎯 Problem

Professionals often spend significant time:

- Sorting through large inboxes
- Identifying urgent or actionable emails
- Determining which messages require a response
- Writing repetitive replies
- Coordinating meetings
- Following up on important communication

Traditional automation works well for simple rules, but email intent is often contextual.

For example:

> "Would Thursday afternoon work for a quick conversation?"

Understanding this message requires more than keyword matching.

The system needs to understand that:

- The sender may be proposing a meeting
- The sender is suggesting a possible time
- The recipient may need to respond
- Calendar availability may become relevant

**Chief of Staff Application** uses LLMs where contextual understanding provides value while keeping deterministic controls around important actions.

---

# 💡 Product Vision

> **Turn an overloaded inbox into a prioritized action queue while keeping the human in control.**

The core product principle is:

```text
AI recommends
      ↓
Human reviews
      ↓
Human approves
      ↓
System executes
```

This creates a **human-in-the-loop AI workflow** where AI reduces repetitive work without removing human accountability.

---

# ✨ Key Features

## 📥 1. Intelligent Inbox & AI Triage

Transforms a traditional inbox into a **prioritized action queue**.

The system analyzes email threads and identifies:

- Priority
- Email category
- Whether a response is required
- Recommended action
- Deadline
- Confidence level
- Reason for the recommendation
- Why the email matters

### Priority Classification

| Priority | Meaning |
|---|---|
| 🔴 **Urgent** | Requires immediate attention |
| 🟠 **Needs Reply** | Requires a response |
| 🟡 **Important** | Important but not immediately actionable |
| 🔵 **FYI** | Useful information with no action required |
| ⚪ **Low Priority** | Low-value or non-urgent communication |
| ⚫ **Spam** | Unwanted or irrelevant communication |

This converts an unstructured inbox into a **structured decision queue**.

---

## 🧠 2. Context-Aware AI Classification

The AI evaluates email intent using **message context** rather than relying only on keywords or static rules.

The system can distinguish between:

- Meeting requests
- Task requests
- Recruiter outreach
- Job opportunities
- Follow-ups
- Newsletters
- Promotional messages
- Billing messages
- Administrative communication
- Social messages
- Spam

This allows the AI to determine not only **what an email is**, but also **what the user should potentially do about it**.

### Example

| Field | Classification |
|---|---|
| **Priority** | Needs Reply |
| **Category** | Job Opportunity |
| **Needs Reply** | Yes |
| **Recommended Action** | Reply |
| **Confidence** | High |

**Reason:**  
The sender is requesting availability for an interview.

**Why It Matters:**  
A response is required to continue the hiring process.

---

## ✍️ 3. AI-Powered Draft Generation

After triage, actionable emails can move into the draft-generation stage.

The AI generates response drafts using:

- Original email content
- Conversation context
- Sender information
- Subject
- Recommended action
- User communication preferences
- Tone and writing style

The goal is not simply to generate grammatically correct text.

The goal is to produce a response that is:

> **Concise + Relevant + Context-aware + Actionable**

---

## 👤 4. Human-in-the-Loop Approval

AI-generated responses are **not automatically sent**.

The workflow is:

```text
AI Recommendation
       ↓
AI Draft
       ↓
Human Review
       ↓
Approve
       ↓
Send
```

The user remains responsible for the final decision.

This is an intentional **product and safety decision** because an incorrect email could affect:

- Professional relationships
- Job opportunities
- Commitments
- Reputation
- Business communication

---

## 📤 5. Controlled Email Sending

Once a user approves a draft, the system can send the response through Gmail.

The application maintains sending state so that:

- Approved drafts can be sent
- Successful sends are recorded
- Failed sends can be retried
- Previously sent messages are not accidentally sent again

The sending capability is therefore **separated from AI generation**.

---

## 📅 6. AI Meeting Request Parsing

The system can interpret meeting-related emails and extract structured information such as:

- Meeting topic
- Proposed times
- Attendees
- Meeting duration

### Example

```text
Meeting Topic:
Design Review

Attendee:
example@company.com

Proposed Time:
Thursday, 2:00 PM

Duration:
60 minutes
```

This converts unstructured email communication into **structured meeting information**.

---

## 🗓️ 7. Calendar Availability & Scheduling

After identifying a meeting request, the system can check calendar availability and identify suitable slots.

The workflow is:

```text
Meeting Request
       ↓
Extract Proposed Times
       ↓
Check Calendar
       ↓
Find Available Slot
       ↓
User Approval
       ↓
Create Event
```

Calendar actions remain controlled rather than allowing the AI to silently create commitments.

---

## 🔄 8. Gmail MCP + Gmail API Architecture

The product supports Gmail integration through multiple execution paths.

### Local Environment

```text
Streamlit
    ↓
Gmail MCP
    ↓
Gmail
```

### Cloud Deployment

```text
Streamlit Cloud
       ↓
Gmail MCP if available
       ↓
Raw Gmail API fallback
       ↓
Gmail
```

This allows the application to remain functional when the local MCP environment is unavailable in a cloud deployment.

---

## 🔐 9. Secure Credential Handling

Sensitive credentials are intentionally excluded from version control.

Examples include:

- `.env`
- `token.json`
- OAuth credentials
- API keys
- Personal email data

Cloud deployments use **runtime secrets** rather than committing credentials to GitHub.

A deployment bootstrap layer maps required secrets into the runtime environment.

---

## ⚡ 10. Cost-Aware AI Workflow

The product avoids generating drafts for every email.

Instead:

```text
100 Inbox Emails
       ↓
    AI Triage
       ↓
20 Actionable Emails
       ↓
Generate 20 Drafts
```

This reduces:

- LLM calls
- Token consumption
- API costs
- Latency
- Rate-limit pressure

This is both a **technical optimization and a product decision**.

---

## 🧪 11. AI & System Evaluation

The project evaluates the workflow across multiple layers.

### AI Evaluation

- Triage accuracy
- Priority accuracy
- Action accuracy
- Structured-output validity
- Draft relevance
- Hallucination rate

### Product Evaluation

- Draft acceptance rate
- Human override rate
- Time saved
- Workflow completion rate

### System Evaluation

- Gmail retrieval reliability
- Email sending reliability
- Calendar scheduling success
- API failure handling
- Model/API latency

---

## 🛡️ 12. AI Failure Handling

AI systems can fail in ways that traditional applications do not.

The product accounts for:

- API rate limits
- Model quota exhaustion
- Authentication failures
- Malformed structured output
- Incomplete model responses
- External API failures

Validation and retry mechanisms are used where appropriate instead of assuming that every AI response will be valid.

---

## 🎯 13. Action-Oriented Inbox

The goal is not simply to create another **AI email summarizer**.

The product converts:

```text
Unstructured Inbox
       ↓
Prioritized Information
       ↓
Recommended Actions
       ↓
AI-Generated Work
       ↓
Human Decision
       ↓
Completed Action
```

This moves the product from an AI email summarizer toward an **AI workflow assistant**.

---

# 🧠 AI Workflow

The complete product workflow is:

```text
                     ┌─────────────────┐
                     │   Gmail Inbox   │
                     └────────┬────────┘
                              ↓
                     ┌─────────────────┐
                     │ Email Retrieval │
                     └────────┬────────┘
                              ↓
                     ┌─────────────────┐
                     │    AI Triage    │
                     └────────┬────────┘
                              ↓
                     ┌─────────────────┐
                     │ Priority /      │
                     │ Action Queue    │
                     └────────┬────────┘
                              ↓
                     ┌─────────────────┐
                     │ Draft Generation│
                     └────────┬────────┘
                              ↓
                     ┌─────────────────┐
                     │ Human Review    │
                     └────────┬────────┘
                              ↓
                     ┌─────────────────┐
                     │ Approval Gate   │
                     └──────┬──┴──────┘
                            ↓
                   ┌────────┴────────┐
                   ↓                 ↓
                Gmail             Calendar
```

---

# 🏗️ AI Based-Decisions

## Why AI?

Traditional rule-based email automation can identify simple patterns.

For example:

```text
Sender = recruiter
Subject contains "interview"
```

However, real communication is often ambiguous.

An email may express intent indirectly.

For example:

> "Would Thursday afternoon work for a quick conversation?"

The system needs to understand:

- This may be a meeting request
- The sender is proposing a time
- The recipient may need to respond
- Calendar availability may be relevant

This is where **LLM-based contextual understanding** provides value.

---

# ⚖️ AI vs Traditional Automation

| Problem | Traditional Rules | AI Approach |
|---|---|---|
| Sender identification | ✅ | |
| Simple filtering | ✅ | |
| Intent detection | Limited | ✅ |
| Priority classification | Limited | ✅ |
| Contextual reply | Templates | ✅ |
| Meeting interpretation | Limited | ✅ |
| Final external action | Automatic risk | Human approval |

The product deliberately combines **deterministic software logic with AI**, rather than using an LLM for everything.

---

# 👤 Human-in-the-Loop Design

A central product decision is separating **AI generation from external execution**.

### AI can:

- Classify emails
- Prioritize communication
- Recommend actions
- Generate response drafts
- Interpret meeting requests
- Identify possible calendar slots

### Human controls:

- Whether the classification is correct
- Whether a draft should be sent
- Whether the response needs editing
- Whether a meeting should be created
- Whether an AI recommendation should be overridden

This creates a **controlled boundary around consequential actions**.

---

# 🛡️ Responsible AI & Guardrails

Because the product interacts with personal and professional communication, safety is part of the product design.

## No Automatic Email Sending

The AI cannot independently send an email simply because it generated a draft.

## No Fabricated Information

The assistant should not invent:

- Work experience
- Education
- Availability
- Application status
- Commitments
- Meeting details

## Consequential Actions Require Approval

Email sending and calendar creation require explicit user interaction.

---

# 💰 Cost & Latency Strategy

Every LLM request can contribute to:

- Token usage
- API cost
- Latency
- Rate-limit consumption

Therefore, the system uses a **staged architecture**.

Instead of:

```text
100 Emails
    ↓
Generate 100 Drafts
```

the product follows:

```text
100 Emails
    ↓
Triage
    ↓
20 Actionable Emails
    ↓
Generate 20 Drafts
```

This makes the system more cost-efficient while focusing AI effort where it provides the most value.

---

# 🔄 Model Strategy

The AI layer is designed to support multiple model providers.

### Current integrations include:

- Google Gemini
- OpenRouter-compatible models

Separating the AI provider from the broader workflow makes it easier to:

- Experiment with different models
- Compare quality
- Manage costs
- Handle rate limits
- Introduce future model providers

---

# 🔧 Reliability Strategy

AI products require explicit handling of failure modes.

The system considers:

```text
Model Failure
      ↓
Validation
      ↓
Retry / Error Handling
      ↓
Clear User Feedback
```

Examples include:

- Rate limits
- API quota exhaustion
- Authentication failures
- Malformed JSON
- Incomplete model responses
- Gmail API failures
- Calendar API failures

The goal is to prevent an AI failure from becoming an **unexplained product failure**.

---

# 🏗️ System Architecture

```text
                     ┌─────────────────────┐
                     │     Streamlit UI    │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │  Inbox & Triage     │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │   Gmail Engine      │
                     │                     │
                     │ MCP / Gmail API     │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │     AI Triage       │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │  Draft Generation   │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │ Human Approval Gate │
                     └──────────┬──────────┘
                                │
                       ┌────────┴────────┐
                       ▼                 ▼
                    Gmail             Calendar
```

---

# 📊 Evaluation & Metrics

The product can be evaluated at three levels.

## 1. AI Quality

### Triage Accuracy

Percentage of emails correctly classified.

```text
Correct Classifications
----------------------- × 100
Total Evaluated Emails
```

### Draft Acceptance Rate

Percentage of AI drafts accepted by users.

```text
Approved Drafts
-------------- × 100
Generated Drafts
```

### Human Override Rate

Percentage of AI recommendations changed by the user.

A high override rate can indicate that the model or prompt requires improvement.

---

## 2. Product Quality

Potential product metrics include:

| Metric | Purpose |
|---|---|
| Emails processed | Usage |
| Actionable email rate | Inbox complexity |
| Draft acceptance rate | AI usefulness |
| Human override rate | Trust / AI quality |
| Time saved | User value |
| Workflow completion rate | Product effectiveness |

---

## 3. System Quality

| Metric | Purpose |
|---|---|
| Gmail fetch success rate | Integration reliability |
| Email send success rate | Execution reliability |
| Calendar scheduling success | Workflow reliability |
| API failure rate | System stability |
| Average latency | User experience |
| AI cost per email | Unit economics |

---

# 🔐 Deployment Architecture

## Local Development

```text
Developer
    ↓
Streamlit
    ↓
Gmail MCP
    ↓
Gmail
```

## Cloud Deployment

```text
User
 ↓
Streamlit Cloud
 ↓
Gmail MCP if available
 ↓
Raw Gmail API fallback
 ↓
Gmail
```

The application does not assume that local development tools will exist in production.

Deployment credentials are provided through **runtime secrets**.

---

# 🧰 Technology Stack

| Layer | Technology |
|---|---|
| **Frontend** | Streamlit |
| **Backend** | Python |
| **Email Integration** | Gmail API |
| **MCP Integration** | Gmail MCP |
| **AI** | Google Gemini |
| **AI Provider Gateway** | OpenRouter |
| **Authentication** | Google OAuth 2.0 |
| **Version Control** | Git / GitHub |
| **Runtime / Tooling** | Node.js / npm |
| **Deployment** | Streamlit Cloud |

---

# 🔄 Development Phases

The product was developed incrementally instead of attempting to build the complete assistant at once.

## Phase 1 — Inbox & Triage

### Focus

- Gmail integration
- Email retrieval
- AI classification
- Priority classification
- Action queue

---

## Phase 2 — Draft Generation

### Focus

- Identify actionable threads
- Generate contextual replies
- Display original email alongside AI draft
- Improve response quality

---

## Phase 3 — Approval & Sending

### Focus

- Human approval gate
- Controlled email sending
- Sent-state tracking
- Retry handling
- Execution safety

---

## Phase 4 — Calendar & Meeting Coordination

### Focus

- Meeting request parsing
- Attendee extraction
- Proposed time extraction
- Calendar availability
- Event creation workflow

---

# 🧪 Testing & Validation

The project includes testing across core product workflows.

### Validated Areas

- Gmail retrieval
- AI triage
- Draft generation
- Email sending
- Calendar availability
- Calendar event creation
- Meeting request parsing
- Error handling
- Deployment fallback behavior

The architecture also separates **read-only operations from consequential actions** to reduce testing risk.

---

# 📁 Project Structure

```text
CHIEF_OF_STAFF/
│
├── app.py
├── engine.py
├── triage.py
├── draft_machine.py
├── context_builder.py
├── calendar_engine.py
├── mailer.py
├── bootstrap.py
│
├── requirements.txt
├── .gitignore
└── README.md
```

Sensitive credentials and personal email data are intentionally excluded from version control.

---

# 🚀 Future Roadmap

## Near Term

- [ ] Improve triage accuracy
- [ ] Improve draft editing experience
- [ ] Add user feedback for AI classifications
- [ ] Improve model fallback
- [ ] Expand evaluation dashboards
- [ ] Improve observability

## Medium Term

- [ ] Multi-account email support
- [ ] Work/personal inbox separation
- [ ] Follow-up tracking
- [ ] Deadline detection
- [ ] Personalized AI preferences
- [ ] User-specific learning from corrections

## Long Term

- [ ] Daily executive briefing
- [ ] Meeting preparation
- [ ] Post-meeting follow-up
- [ ] Cross-platform communication management
- [ ] Proactive task management
- [ ] Personalized decision support

---


# 👤 Author

**Indemmity Lamare**

### Aspiring AI Product Manager

Interested in building AI products at the intersection of:

**Product Management × AI/ML × Automation × Human-Centered Design**
