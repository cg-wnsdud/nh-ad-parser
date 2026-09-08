from document_processor import DocIR

doc = DocIR.from_file(r"C:\Users\cccjj\cginside\repo-analysis\nh-ad-parser\nh-data\template\광고 템플릿(근거규정x, 필수 여부).hwpx")

print(doc.paragraphs[0].text)
print(doc.paragraphs[0].runs[0].run_style.bold)

html = doc.to_html(title="Preview")