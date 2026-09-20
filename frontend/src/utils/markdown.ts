import MarkdownIt from "markdown-it";
import hljs from "highlight.js";
import "highlight.js/styles/github.css";

// 回答用 Markdown 渲染，代码块交给 highlight.js 高亮（与后端提示词约定的 ```lang 代码块对齐）。
export const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  highlight(str: string, lang: string): string {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return hljs.highlight(str, { language: lang }).value;
      } catch {
        /* 落到默认转义 */
      }
    }
    return "";
  },
});

export function renderMarkdown(text: string): string {
  return md.render(text || "");
}
