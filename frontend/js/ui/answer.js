import { citationParts } from "./citation.js";

export function answerNotice(answer) {
  if (answer.found === false) return "No supporting code found in this repo";
  return answer.confidence === "low" ? "Low confidence: verify against the cited code." : "";
}

export function appendAnswerText(root, text, citations, chip) {
  const appendPlain = (value) => citationParts(value, citations).forEach((part) => {
    if (part.citation) root.append(chip(part.citation, part.text));
    else {
      const span = document.createElement("span"); span.textContent = part.text; root.append(span);
    }
  });
  let cursor = 0;
  // Only paired single backticks on one line are inline code; leave other text literal.
  for (const match of text.matchAll(/(?<!`)`([^`\r\n]+)`(?!`)/g)) {
    appendPlain(text.slice(cursor, match.index));
    const code = document.createElement("code"); code.textContent = match[1]; root.append(code);
    cursor = match.index + match[0].length;
  }
  appendPlain(text.slice(cursor));
}
