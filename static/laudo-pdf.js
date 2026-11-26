/* Three Health — documento do laudo para impressão / PDF.
 *
 * Uso único nas páginas de resultado, laudo salvo e teste gratuito:
 *   openLaudoPdf({
 *     reportHTML,                       // HTML do laudo já renderizado na página
 *     generatedAt: '05/10/2026 14:32',  // texto pronto
 *     documentId: 'a1b2c3',             // opcional — identificador do laudo
 *     badge: 'Teste gratuito',          // opcional
 *     responsible: { name, role, register, organization },  // opcional
 *     meta: [{ label, value }],         // opcional — região, referências, etc.
 *     clinicalInfo: 'Paciente: 38 anos | Lateralidade: Direito',  // opcional
 *     images: [{ src, label }],         // opcional (já com os achados marcados)
 *     findings: [{ numero, imagem, estrutura, local, certeza }],  // opcional
 *     lang: 'pt-BR' | 'en',             // opcional — idioma do laudo (rótulos e aviso legal)
 *   });
 *
 * Todo texto vindo do usuário é escapado; apenas reportHTML entra como HTML.
 */
(function () {
    'use strict';

    const CSS = `
@import url('https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
:root { --primary: #1B72E8; --primary-dark: #155FC4; --green: #22C97A; --text: #1e293b; --muted: #64748b; --border: #e2e8f0; --soft: #f4f7fb; }
body { font-family: 'Nunito', Arial, sans-serif; font-size: 12.5px; color: var(--text); background: #fff; line-height: 1.55; }
.page { max-width: 780px; margin: 0 auto; padding: 32px 36px; }

/* Cabeçalho */
.doc-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; border-bottom: 3px solid var(--primary); padding-bottom: 14px; margin-bottom: 18px; }
.logo-row { display: flex; align-items: center; gap: 8px; }
.logo-lines { display: flex; flex-direction: column; gap: 3px; }
.logo-lines span { display: block; height: 3px; border-radius: 2px; background: var(--green); }
.logo-lines span:nth-child(1) { width: 22px; }
.logo-lines span:nth-child(2) { width: 16px; }
.logo-lines span:nth-child(3) { width: 11px; }
.logo-wordmark { font-size: 22px; font-weight: 800; color: var(--primary); letter-spacing: -0.5px; line-height: 1; }
.logo-underline { width: 100%; height: 2px; background: var(--primary); margin-top: 3px; }
.badge { display: inline-block; margin-top: 6px; background: #fef3c7; border: 1px solid #f59e0b; border-radius: 4px; padding: 1px 7px; font-size: 9.5px; font-weight: 800; color: #78350f; text-transform: uppercase; letter-spacing: 0.04em; }
.doc-title-block { text-align: right; }
.doc-title { font-size: 15px; font-weight: 800; color: var(--text); letter-spacing: 0.01em; }
.doc-subtitle { font-size: 10.5px; color: var(--muted); margin-top: 3px; }

/* Identificação */
.ident { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px 18px; background: var(--soft); border-radius: 8px; padding: 12px 16px; margin-bottom: 20px; }
.ident-wide { grid-column: 1 / -1; }
.label { display: block; font-size: 9.5px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.value { display: block; font-size: 12px; font-weight: 700; color: var(--text); }
.value-strong { font-size: 15px; font-weight: 800; }
.value-sub { display: block; font-size: 11px; color: var(--muted); font-weight: 600; }

/* Seções numeradas */
.section { margin-bottom: 18px; }
.section-title { display: flex; align-items: center; gap: 8px; font-size: 11.5px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.06em; color: var(--primary); border-bottom: 1px solid var(--border); padding-bottom: 5px; margin-bottom: 10px; }
.section-num { display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; border-radius: 50%; background: var(--primary); color: #fff; font-size: 10px; letter-spacing: 0; }

.clinical-list { list-style: none; display: grid; gap: 3px; }
.clinical-list li { font-size: 12px; }
.clinical-list strong { color: var(--text); }

.images { display: flex; flex-wrap: wrap; gap: 10px; }
.images figure { flex: 0 0 auto; }
.images img { display: block; max-width: 100%; max-height: 240px; object-fit: contain; border-radius: 6px; border: 1px solid var(--border); background: #0a0a0a; }
.laudo-table-wrap { margin: 6px 0 10px; }
.laudo-table { width: 100%; border-collapse: collapse; font-size: 10.5px; }
.laudo-table th, .laudo-table td { border: 1px solid var(--border); padding: 3px 5px; text-align: left; vertical-align: top; }
.laudo-table th { background: var(--soft); font-weight: 800; }
.findings { list-style: none; display: grid; gap: 6px; }
.findings li { display: flex; gap: 8px; align-items: flex-start; }
.f-num { flex: 0 0 18px; height: 18px; border-radius: 50%; color: #fff; font-weight: 800; font-size: 10px; display: inline-flex; align-items: center; justify-content: center; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.f-level { border: 1.5px solid; border-radius: 999px; padding: 0 6px; font-size: 9.5px; font-weight: 800; color: var(--muted); }
.f-local { color: var(--muted); font-size: 11.5px; }
.f-note { margin-top: 8px; font-size: 10.5px; color: var(--muted); }
.images figcaption { text-align: center; font-size: 10.5px; color: var(--muted); margin-top: 4px; }

.report { font-size: 12.5px; line-height: 1.7; }
.report h2 { font-size: 13.5px; color: var(--primary); margin: 14px 0 5px; }
.report h3 { font-size: 13px; color: var(--primary); margin: 12px 0 4px; }
.report h4 { font-size: 12px; color: var(--primary-dark); margin: 8px 0 3px; }
.report ul, .report ol { padding-left: 18px; margin: 4px 0; }
.report li { margin-bottom: 2px; }
.report p { margin: 4px 0; }
.report .laudo-alertas { margin: 0 0 10px; padding: 8px 12px; border: 1px solid #86efac; background: #f0fdf4; border-radius: 8px; break-inside: avoid; }
.report .laudo-alertas.has-alert { border: 2px solid #dc2626; background: #fef2f2; }
.report .laudo-alertas.has-alert h3 { color: #b91c1c; }
.support-banner { margin: 0 0 14px; padding: 8px 12px; border: 1px solid #f59e0b; background: #fffbeb; border-radius: 8px; font-size: 11.5px; line-height: 1.5; color: #78350f; }
.report strong { color: #0f172a; }
.report blockquote { margin: 6px 0; padding: 6px 10px; border-left: 3px solid var(--primary); background: var(--soft); color: var(--muted); break-inside: avoid; }

/* Validação e assinatura */
.validation { break-inside: avoid; page-break-inside: avoid; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; margin-top: 22px; }
.validation-text { font-size: 11px; color: var(--muted); margin-bottom: 26px; }
.sign-grid { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 18px; }
.sign-line { border-top: 1px solid #94a3b8; padding-top: 4px; font-size: 10px; color: var(--muted); }

/* Rodapé */
.doc-footer { break-inside: avoid; page-break-inside: avoid; margin-top: 18px; padding-top: 10px; border-top: 2px solid var(--primary); display: flex; gap: 16px; align-items: flex-start; }
.doc-footer-sig { flex: 0 0 auto; font-size: 11px; font-weight: 800; color: var(--primary); }
.doc-footer-sig > span { display: block; font-size: 10px; color: var(--muted); font-weight: 400; margin-top: 6px; }
.footer-logo { display: inline-flex; flex-direction: column; align-items: flex-start; }
.footer-logo .logo-row { gap: 6px; }
.footer-logo .logo-lines { gap: 2px; }
.footer-logo .logo-lines span { height: 2px; }
.footer-logo .logo-lines span:nth-child(1) { width: 15px; }
.footer-logo .logo-lines span:nth-child(2) { width: 11px; }
.footer-logo .logo-lines span:nth-child(3) { width: 8px; }
.footer-logo .logo-wordmark { font-size: 15px; }
.footer-logo .logo-underline { height: 1.5px; margin-top: 2px; }
.disclaimer { flex: 1; font-size: 9.5px; color: #78350f; background: #fefce8; border: 1px solid #fde68a; border-radius: 6px; padding: 7px 10px; line-height: 1.5; }

@page { margin: 14mm 12mm; }
@media print {
  .page { padding: 0; max-width: none; }
  body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
`;

    function esc(value) {
        return String(value ?? '').replace(/[&<>"']/g, ch => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[ch]));
    }

    // "Paciente: 38 anos | Lateralidade: Direito" → lista "rótulo: valor"
    function clinicalListHTML(text) {
        const items = String(text).split(/\s*\|\s*/).filter(Boolean).map(part => {
            const idx = part.indexOf(':');
            if (idx > 0 && idx < 40) {
                return `<li><strong>${esc(part.slice(0, idx))}:</strong> ${esc(part.slice(idx + 1).trim())}</li>`;
            }
            return `<li>${esc(part)}</li>`;
        });
        return `<ul class="clinical-list">${items.join('')}</ul>`;
    }

    const FINDING_COLORS = { forte: '#16a34a', moderado: '#eab308', baixo: '#dc2626' };

    // Textos fixos do documento. O laudo em inglês (core/idioma.py) leva rótulos
    // e aviso legal em inglês, sem as normas brasileiras (CFM, COFFITO, LGPD).
    const TEXT = {
        'pt-BR': {
            htmlLang: 'pt-BR',
            levels: { forte: 'Forte grau de certeza', moderado: 'Moderado grau de certeza', baixo: 'Baixo grau de certeza' },
            noResponsible: 'Responsável não informado',
            responsible: 'Responsável pela análise',
            issued: 'Data de emissão',
            reportNo: 'Nº do laudo',
            clinicalInfo: 'Informações clínicas fornecidas',
            image: 'Imagem',
            imageAlt: i => `Imagem ${i} do exame`,
            examImage: 'Imagem do exame',
            examImages: 'Imagens do exame',
            findings: 'Achados marcados nas imagens',
            findingsNote: 'Verde: forte grau de certeza · Amarelo: moderado · Vermelho: baixo. A posição das marcações é estimada pela IA e aproximada.',
            analysis: 'Análise de apoio por inteligência artificial',
            docTitle: 'Laudo Three Health',
            subtitle: 'Ortopedia · Sistema musculoesquelético',
            banner: '<strong>Documento de apoio gerado por IA — não é laudo radiológico nem diagnóstico médico.</strong> Baseado apenas nas imagens enviadas (em RM/TC, cortes selecionados, não o estudo completo). Deve ser revisado por profissional habilitado antes de qualquer decisão.',
            validationTitle: 'Validação do profissional responsável',
            validationText: 'Declaro que revisei as imagens e o conteúdo desta análise, e que a conduta adotada, dentro das atribuições da minha profissão, é de minha responsabilidade.',
            sign: ['Assinatura e carimbo', 'Registro profissional (CRM, CREFITO…)', 'Data'],
            generatedBy: d => `Gerado por IA em ${d}`,
            disclaimer: '<strong>AVISO CFM/COFFITO/LGPD:</strong> Documento gerado por Inteligência Artificial como ferramenta de apoio à decisão clínica. <strong>Não substitui</strong> o laudo radiológico, o diagnóstico nem a conduta do médico responsável: o diagnóstico por imagem e a indicação de cirurgia ou medicamento são atos médicos (Lei 12.842/2013). Fisioterapeutas o utilizam, dentro das atribuições definidas pelo COFFITO e do Código de Ética e Deontologia da Fisioterapia (Resolução COFFITO nº 424/2013), para planejar a reabilitação e decidir o encaminhamento. O uso da IA deve ser informado ao paciente, os dados devem ser tratados conforme a LGPD e a decisão final deve ser registrada pelo profissional habilitado.',
            popupBlocked: 'O navegador bloqueou a janela do PDF. Permita pop-ups para este site e tente de novo.',
        },
        en: {
            htmlLang: 'en',
            levels: { forte: 'High certainty', moderado: 'Moderate certainty', baixo: 'Low certainty' },
            noResponsible: 'Responsible professional not provided',
            responsible: 'Responsible professional',
            issued: 'Issued on',
            reportNo: 'Report no.',
            clinicalInfo: 'Clinical information provided',
            image: 'Image',
            imageAlt: i => `Exam image ${i}`,
            examImage: 'Exam image',
            examImages: 'Exam images',
            findings: 'Findings marked on the images',
            findingsNote: 'Green: high certainty · Yellow: moderate · Red: low. Mark positions are estimated by the AI and approximate.',
            analysis: 'AI-assisted analysis',
            docTitle: 'Three Health report',
            subtitle: 'Orthopedics · Musculoskeletal system',
            banner: '<strong>AI-generated support document — not a radiology report or a medical diagnosis.</strong> Based only on the images provided (for MRI/CT, selected slices, not the full study). It must be reviewed by a licensed professional before any decision.',
            validationTitle: 'Review by the responsible professional',
            validationText: 'I declare that I have reviewed the images and the content of this analysis, and that the management adopted, within the scope of my profession, is my responsibility.',
            sign: ['Signature', 'Professional license no.', 'Date'],
            generatedBy: d => `AI-generated on ${d}`,
            disclaimer: '<strong>NOTICE:</strong> This document was generated by artificial intelligence as a clinical decision-support tool. It <strong>does not replace</strong> the radiology report, the diagnosis or the management decided by the responsible physician. Diagnosis, imaging interpretation and the indication of surgery or medication remain the responsibility of licensed professionals, within the scope of practice defined by local regulations. Patients should be informed that AI was used, personal health data must be handled under the applicable data-protection law (e.g. GDPR, HIPAA), and the final decision must be recorded by the licensed professional.',
            popupBlocked: 'The browser blocked the PDF window. Allow pop-ups for this site and try again.',
        },
    };

    function textFor(lang) {
        return TEXT[lang === 'en' ? 'en' : 'pt-BR'];
    }

    function buildLaudoHTML(opts) {
        const t = textFor(opts.lang);
        const responsible = opts.responsible || {};
        const responsibleName = responsible.name || t.noResponsible;
        const responsibleDetails = [responsible.role, responsible.register, responsible.organization].filter(Boolean).join(' · ');
        const images = (opts.images || []).filter(img => img && img.src);
        const meta = (opts.meta || []).filter(m => m && m.value);

        const identItems = [
            `<div class="ident-wide"><span class="label">${esc(t.responsible)}</span>
                <span class="value value-strong">${esc(responsibleName)}</span>
                ${responsibleDetails ? `<span class="value-sub">${esc(responsibleDetails)}</span>` : ''}</div>`,
            `<div><span class="label">${esc(t.issued)}</span><span class="value">${esc(opts.generatedAt)}</span></div>`,
            ...(opts.documentId ? [`<div><span class="label">${esc(t.reportNo)}</span><span class="value">${esc(opts.documentId)}</span></div>`] : []),
            ...meta.map(m => `<div><span class="label">${esc(m.label)}</span><span class="value">${esc(m.value)}</span></div>`),
        ];

        const sections = [];
        if (opts.clinicalInfo) {
            sections.push([t.clinicalInfo, clinicalListHTML(opts.clinicalInfo)]);
        }
        if (images.length) {
            const figures = images.map((img, i) => `<figure><img src="${esc(img.src)}" alt="${esc(t.imageAlt(i + 1))}">${
                images.length > 1 ? `<figcaption>${esc(img.label || `${t.image} ${i + 1}`)}</figcaption>` : ''}</figure>`).join('');
            sections.push([images.length === 1 ? t.examImage : t.examImages, `<div class="images">${figures}</div>`]);
        }
        const findings = (opts.findings || []).filter(f => f && FINDING_COLORS[f.certeza]);
        if (findings.length) {
            const rows = findings.map(f => `<li><span class="f-num" style="background:${FINDING_COLORS[f.certeza]}">${esc(f.numero)}</span>
                <div><strong>${esc(f.estrutura)}</strong> <span class="f-level" style="border-color:${FINDING_COLORS[f.certeza]}">${esc(t.levels[f.certeza])}</span>
                <div class="f-local">${images.length > 1 ? `${t.image} ${esc(f.imagem)} · ` : ''}${esc(f.local)}</div></div></li>`).join('');
            sections.push([t.findings, `<ol class="findings">${rows}</ol>
                <p class="f-note">${esc(t.findingsNote)}</p>`]);
        }
        sections.push([t.analysis, `<div class="report">${opts.reportHTML || ''}</div>`]);

        const sectionsHTML = sections.map(([title, body], i) => `
    <section class="section">
        <h2 class="section-title"><span class="section-num">${i + 1}</span>${esc(title)}</h2>
        ${body}
    </section>`).join('');

        return `<!DOCTYPE html>
<html lang="${t.htmlLang}">
<head>
<meta charset="UTF-8">
<title>${esc(t.docTitle)} — ${esc(opts.generatedAt)}</title>
<style>${CSS}</style>
</head>
<body>
<div class="page">
    <header class="doc-header">
        <div>
            <div class="logo-row">
                <div class="logo-lines"><span></span><span></span><span></span></div>
                <span class="logo-wordmark">health</span>
            </div>
            <div class="logo-underline"></div>
            ${opts.badge ? `<span class="badge">${esc(opts.badge)}</span>` : ''}
        </div>
        <div class="doc-title-block">
            <div class="doc-title">${esc(t.analysis)}</div>
            <div class="doc-subtitle">${esc(t.subtitle)}</div>
        </div>
    </header>

    <div class="support-banner">${t.banner}</div>
    <div class="ident">${identItems.join('')}</div>
    ${sectionsHTML}

    <div class="validation">
        <h2 class="section-title" style="margin-bottom:6px">${esc(t.validationTitle)}</h2>
        <p class="validation-text">${esc(t.validationText)}</p>
        <div class="sign-grid">
            ${t.sign.map(label => `<div class="sign-line">${esc(label)}</div>`).join('')}
        </div>
    </div>

    <footer class="doc-footer">
        <div class="doc-footer-sig">
            <div class="footer-logo">
                <div class="logo-row">
                    <div class="logo-lines"><span></span><span></span><span></span></div>
                    <span class="logo-wordmark">health</span>
                </div>
                <div class="logo-underline"></div>
            </div>
            <span>${esc(t.generatedBy(opts.generatedAt))}</span>
        </div>
        <div class="disclaimer">${t.disclaimer}</div>
    </footer>
</div>
<script>window.onload = function () { window.print(); };<\/script>
</body>
</html>`;
    }

    function openLaudoPdf(opts) {
        const win = window.open('', '_blank', 'width=900,height=700');
        if (!win) {
            if (window.showToast) {
                window.showToast(textFor(opts.lang).popupBlocked, 'warning', 6000);
            }
            return;
        }
        win.document.write(buildLaudoHTML(opts));
        win.document.close();
    }

    function nowBR(lang) {
        const now = new Date();
        if (lang === 'en') {
            const pad = n => String(n).padStart(2, '0');
            return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
        }
        const date = now.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric' });
        const time = now.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        return `${date} às ${time}`;
    }

    window.openLaudoPdf = openLaudoPdf;
    window.buildLaudoHTML = buildLaudoHTML;
    window.laudoNowBR = nowBR;
})();
