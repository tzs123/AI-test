package agent

import (
	"bufio"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"unicode"
)

type Knowledge struct {
	ID       string                 `json:"id"`
	Title    string                 `json:"title"`
	Type     string                 `json:"type"`
	Content  string                 `json:"content"`
	Source   string                 `json:"source,omitempty"`
	Metadata map[string]interface{} `json:"metadata,omitempty"`
	Score    float64                `json:"score,omitempty"`
}

// DefectLoader is implemented by a service/repository that reads historical
// defects from MySQL through GORM or database/sql.
type DefectLoader interface{ LoadDefects() ([]Knowledge, error) }

// MySQLDefectLoader adapts an existing defects table to the knowledge base.
type MySQLDefectLoader struct {
	DB    *sql.DB
	Query string
}

func (l MySQLDefectLoader) LoadDefects() ([]Knowledge, error) {
	if l.DB == nil {
		return nil, errors.New("mysql db is nil")
	}
	query := l.Query
	if query == "" {
		query = "SELECT id, title, description, severity, status FROM defects ORDER BY id DESC LIMIT 1000"
	}
	rows, err := l.DB.QueryContext(context.Background(), query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Knowledge
	for rows.Next() {
		var id, title, description, severity, status string
		if err := rows.Scan(&id, &title, &description, &severity, &status); err != nil {
			return nil, err
		}
		out = append(out, Knowledge{ID: "defect-" + id, Title: title, Type: "bug", Content: description, Metadata: map[string]interface{}{"severity": severity, "status": status, "id": id}})
	}
	return out, rows.Err()
}

type KnowledgeBase struct {
	mu        sync.RWMutex
	documents []Knowledge
	defects   []Knowledge
	standards []Knowledge
}

func NewKnowledgeBase() *KnowledgeBase { return &KnowledgeBase{} }

func (kb *KnowledgeBase) Add(k Knowledge) {
	k.Content = strings.TrimSpace(k.Content)
	if k.Content == "" {
		return
	}
	kb.mu.Lock()
	defer kb.mu.Unlock()
	if k.ID == "" {
		k.ID = fmt.Sprintf("knowledge-%d", len(kb.documents)+len(kb.defects)+len(kb.standards)+1)
	}
	switch strings.ToLower(k.Type) {
	case "bug", "defect", "历史缺陷":
		kb.defects = append(kb.defects, k)
	case "standard", "规范", "yaml":
		kb.standards = append(kb.standards, k)
	default:
		kb.documents = append(kb.documents, k)
	}
}

func (kb *KnowledgeBase) LoadDocument(path string) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	text := string(b)
	ext := strings.ToLower(filepath.Ext(path))
	if ext == ".pdf" {
		text = extractPDFText(b)
	}
	if strings.TrimSpace(text) == "" {
		return errors.New("knowledge document is empty")
	}
	title := strings.TrimSuffix(filepath.Base(path), ext)
	if ext == ".md" || ext == ".markdown" {
		if m := regexp.MustCompile(`(?m)^#\s+(.+)$`).FindStringSubmatch(text); len(m) > 1 {
			title = strings.TrimSpace(m[1])
		}
	}
	kb.Add(Knowledge{ID: "doc-" + title, Title: title, Type: "document", Content: text, Source: path})
	return nil
}

// LoadDocuments recursively loads Markdown and PDF files from a directory.
func (kb *KnowledgeBase) LoadDocuments(dir string) error {
	return filepath.Walk(dir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return nil
		}
		ext := strings.ToLower(filepath.Ext(path))
		if ext == ".md" || ext == ".markdown" || ext == ".pdf" {
			return kb.LoadDocument(path)
		}
		return nil
	})
}

func (kb *KnowledgeBase) LoadHistoricalDefects(loader DefectLoader) error {
	if loader == nil {
		return errors.New("defect loader is nil")
	}
	items, err := loader.LoadDefects()
	if err != nil {
		return err
	}
	kb.mu.Lock()
	defer kb.mu.Unlock()
	kb.defects = append(kb.defects, items...)
	return nil
}

// LoadStandardsYAML handles the common standards YAML shape without requiring
// a YAML runtime: either a list of objects or key/value rules is accepted.
func (kb *KnowledgeBase) LoadStandardsYAML(path string) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	items := parseSimpleYAML(string(b))
	if len(items) == 0 {
		return errors.New("no standards found in yaml")
	}
	for i, item := range items {
		title := item["name"]
		if title == "" {
			title = item["title"]
		}
		if title == "" {
			title = fmt.Sprintf("standard-%d", i+1)
		}
		content := item["content"]
		if content == "" {
			content = item["rule"]
		}
		if content == "" {
			content = strings.TrimSpace(string(b))
		}
		kb.Add(Knowledge{ID: "standard-" + strconv.Itoa(i+1), Title: title, Type: "standard", Content: content, Source: path, Metadata: map[string]interface{}{"fields": item}})
	}
	return nil
}

func (kb *KnowledgeBase) Retrieve(query string) []Knowledge {
	q := tokenize(query)
	if len(q) == 0 {
		return nil
	}
	kb.mu.RLock()
	all := append(append(append([]Knowledge{}, kb.documents...), kb.defects...), kb.standards...)
	kb.mu.RUnlock()
	type scored struct {
		k     Knowledge
		score float64
	}
	ranked := make([]scored, 0, len(all))
	for _, k := range all {
		words := tokenize(k.Title + " " + k.Content)
		if len(words) == 0 {
			continue
		}
		hits := 0
		for _, term := range q {
			for _, word := range words {
				if word == term || strings.Contains(word, term) {
					hits++
					break
				}
			}
		}
		if hits > 0 {
			k.Score = float64(hits) / float64(len(q))
			ranked = append(ranked, scored{k, k.Score})
		}
	}
	sort.SliceStable(ranked, func(i, j int) bool { return ranked[i].score > ranked[j].score })
	out := make([]Knowledge, 0, len(ranked))
	for _, x := range ranked {
		out = append(out, x.k)
	}
	return out
}

func (kb *KnowledgeBase) PromptContext(query string) string {
	items := kb.Retrieve(query)
	if len(items) == 0 {
		return ""
	}
	var b strings.Builder
	for i, k := range items {
		if i >= 5 {
			break
		}
		fmt.Fprintf(&b, "[%s] %s\n%s\n", k.Type, k.Title, k.Content)
	}
	return b.String()
}

func tokenize(s string) []string {
	var out []string
	for _, part := range strings.FieldsFunc(strings.ToLower(s), func(r rune) bool { return unicode.IsSpace(r) || unicode.IsPunct(r) }) {
		if len([]rune(part)) >= 2 {
			out = append(out, part)
		}
	}
	return out
}

func parseSimpleYAML(raw string) []map[string]string {
	var out []map[string]string
	var current map[string]string
	s := bufio.NewScanner(strings.NewReader(raw))
	for s.Scan() {
		line := strings.TrimSpace(s.Text())
		if line == "" || strings.HasPrefix(line, "#") || line == "---" {
			continue
		}
		if strings.HasPrefix(line, "-") {
			if current != nil {
				out = append(out, current)
			}
			current = map[string]string{}
			line = strings.TrimSpace(strings.TrimPrefix(line, "-"))
			if line == "" {
				continue
			}
		}
		if current == nil {
			current = map[string]string{}
		}
		p := strings.Index(line, ":")
		if p < 1 {
			continue
		}
		key := strings.TrimSpace(line[:p])
		val := strings.Trim(strings.TrimSpace(line[p+1:]), "\"'")
		current[key] = val
	}
	if current != nil && len(current) > 0 {
		out = append(out, current)
	}
	return out
}

func extractPDFText(data []byte) string {
	// PDF text operators commonly contain literal strings in parentheses. This
	// conservative extraction is dependency-free and sufficient for retrieval;
	// deployments needing layout fidelity can replace LoadDocument's parser.
	re := regexp.MustCompile(`\(([^()]*)\)`)
	matches := re.FindAllSubmatch(data, -1)
	var parts []string
	for _, m := range matches {
		if len(m) > 1 {
			parts = append(parts, string(m[1]))
		}
	}
	return strings.Join(parts, " ")
}
