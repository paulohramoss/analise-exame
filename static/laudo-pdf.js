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
 *     images: [{ src, label }],         // opcional
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
.images figcaption { text-align: center; font-size: 10.5px; color: var(--muted); margin-top: 4px; }

.report { font-size: 12.5px; line-height: 1.7; }
.report h2 { font-size: 13.5px; color: var(--primary); margin: 14px 0 5px; }
.report h3 { font-size: 13px; color: var(--primary); margin: 12px 0 4px; }
.report h4 { font-size: 12px; color: var(--primary-dark); margin: 8px 0 3px; }
.report ul, .report ol { padding-left: 18px; margin: 4px 0; }
.report li { margin-bottom: 2px; }
.report p { margin: 4px 0; }
.report strong { color: #0f172a; }

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

    function buildLaudoHTML(opts) {
        const responsible = opts.responsible || {};
        const responsibleName = responsible.name || 'Responsável não informado';
        const responsibleDetails = [responsible.role, responsible.register, responsible.organization].filter(Boolean).join(' · ');
        const images = (opts.images || []).filter(img => img && img.src);
        const meta = (opts.meta || []).filter(m => m && m.value);

        const identItems = [
            `<div class="ident-wide"><span class="label">Responsável pela análise</span>
                <span class="value value-strong">${esc(responsibleName)}</span>
                ${responsibleDetails ? `<span class="value-sub">${esc(responsibleDetails)}</span>` : ''}</div>`,
            `<div><span class="label">Data de emissão</span><span class="value">${esc(opts.generatedAt)}</span></div>`,
            ...(opts.documentId ? [`<div><span class="label">Nº do laudo</span><span class="value">${esc(opts.documentId)}</span></div>`] : []),
            ...meta.map(m => `<div><span class="label">${esc(m.label)}</span><span class="value">${esc(m.value)}</span></div>`),
        ];

        const sections = [];
        if (opts.clinicalInfo) {
            sections.push(['Informações clínicas fornecidas', clinicalListHTML(opts.clinicalInfo)]);
        }
        if (images.length) {
            const figures = images.map((img, i) => `<figure><img src="${esc(img.src)}" alt="Imagem ${i + 1} do exame">${
                images.length > 1 ? `<figcaption>${esc(img.label || `Imagem ${i + 1}`)}</figcaption>` : ''}</figure>`).join('');
            sections.push([images.length === 1 ? 'Imagem do exame' : 'Imagens do exame', `<div class="images">${figures}</div>`]);
        }
        sections.push(['Análise por inteligência artificial', `<div class="report">${opts.reportHTML || ''}</div>`]);

        const sectionsHTML = sections.map(([title, body], i) => `
    <section class="section">
        <h2 class="section-title"><span class="section-num">${i + 1}</span>${esc(title)}</h2>
        ${body}
    </section>`).join('');

        return `<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Laudo Three Health — ${esc(opts.generatedAt)}</title>
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
            <div class="doc-title">Laudo de análise por inteligência artificial</div>
            <div class="doc-subtitle">Ortopedia · Sistema musculoesquelético</div>
        </div>
    </header>

    <div class="ident">${identItems.join('')}</div>
    ${sectionsHTML}

    <div class="validation">
        <h2 class="section-title" style="margin-bottom:6px">Validação do médico responsável</h2>
        <p class="validation-text">Declaro que revisei as imagens e o conteúdo desta análise, e que a conduta clínica é de minha responsabilidade.</p>
        <div class="sign-grid">
            <div class="sign-line">Assinatura e carimbo</div>
            <div class="sign-line">CRM / registro</div>
            <div class="sign-line">Data</div>
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
            <span>Gerado por IA em ${esc(opts.generatedAt)}</span>
        </div>
        <div class="disclaimer">
            <strong>AVISO CFM/LGPD:</strong> Este laudo foi gerado por Inteligência Artificial como ferramenta de apoio à decisão clínica.
            <strong>Não substitui</strong> diagnóstico, prognóstico, conduta ou laudo emitido pelo médico responsável.
            O uso da IA deve ser informado ao paciente, os dados devem ser tratados conforme a LGPD e a decisão final deve ser registrada pelo profissional habilitado.
        </div>
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
                window.showToast('O navegador bloqueou a janela do PDF. Permita pop-ups para este site e tente de novo.', 'warning', 6000);
            }
            return;
        }
        win.document.write(buildLaudoHTML(opts));
        win.document.close();
    }

    function nowBR() {
        const now = new Date();
        const date = now.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric' });
        const time = now.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        return `${date} às ${time}`;
    }

    window.openLaudoPdf = openLaudoPdf;
    window.buildLaudoHTML = buildLaudoHTML;
    window.laudoNowBR = nowBR;
})();
