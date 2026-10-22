import MarkdownIt from 'markdown-it';

// Model text is untrusted: render Markdown with raw HTML and automatic links disabled.
const markdown = new MarkdownIt({ html: false, breaks: true, linkify: false });

export function formatReviewText(text: string): string {
  // Keep code unchanged when adapting old single-paragraph review results.
  const protectedText: string[] = [];
  const source = (text || '').replace(/\r\n?/g, '\n').replace(
    /```[^]*?```|`[^`\n]+`/g,
    value => `\uE000${protectedText.push(value) - 1}\uE001`,
  );
  const formatted = source
    .replace(/(^|[。；;\n]\s*)(变更意图|主要风险|结论建议|修复建议|触发条件|问题原因|影响|建议)[：:][ \t]*/g,
      '$1\n\n**$2**\n\n')
    .replace(/([：:][ \t]*)(?=1[)）])/g, '$1\n\n')
    .replace(/([；;][ \t]*)(?=\d{1,2}[)）])/g, '$1\n')
    .replace(/(^|\n)[ \t]*(\d{1,2})[)）][ \t]*/g, '$1$2. ')
    .replace(/\uE000(\d+)\uE001/g, (_, index: string) => protectedText[Number(index)] ?? '');
  return markdown.render(formatted);
}
