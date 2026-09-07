package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

// EvaluateQuality computes deterministic metrics and optionally asks an LLM
// for a case quality score. Metrics remain usable when no LLM is configured.
func (e *AgentEngine) EvaluateQuality(results []TestResult) QualityReport {
	r := QualityReport{TotalCases: len(results)}
	for _, result := range results {
		switch strings.ToLower(result.Status) {
		case "passed":
			r.PassedCases++
			r.ExecutedCases++
		case "failed", "error":
			r.FailedCases++
			r.ExecutedCases++
		case "skipped":
			r.SkippedCases++
		}
	}
	if r.TotalCases > 0 {
		r.CoverageRate = float64(r.ExecutedCases) / float64(r.TotalCases)
	}
	if denominator := r.ExecutedCases; denominator > 0 {
		r.DefectDetectionRate = float64(r.FailedCases) / float64(denominator)
	}
	// A useful baseline: passing and failed cases both demonstrate assertions;
	// skipped/error cases reduce quality.
	if r.TotalCases > 0 && r.ExecutedCases > 0 {
		r.CaseQualityScore = clamp(100*(1-float64(r.SkippedCases+r.FailedCases)/float64(r.TotalCases)*0.5), 0, 100)
	}
	// Do not let an LLM score an entirely skipped run. There is no execution
	// evidence to evaluate, so the deterministic baseline and recommendation
	// are more truthful than a model-generated number.
	if e != nil && e.LLMClient != nil && r.ExecutedCases > 0 {
		if score, ok := e.llmQualityScore(results); ok {
			r.CaseQualityScore = score
		}
	}
	r.Summary = fmt.Sprintf("执行 %d/%d，用例通过 %d，失败 %d，覆盖率 %.1f%%", r.ExecutedCases, r.TotalCases, r.PassedCases, r.FailedCases, r.CoverageRate*100)
	if r.FailedCases > 0 {
		r.Recommendations = append(r.Recommendations, "优先分析失败日志并补充异常场景")
	}
	if r.SkippedCases > 0 {
		r.Recommendations = append(r.Recommendations, "补充执行环境或依赖，减少跳过用例")
	}
	return r
}

func (e *AgentEngine) llmQualityScore(results []TestResult) (float64, bool) {
	payload, _ := json.Marshal(results)
	ctx, cancel := e.contextWithTimeout(context.Background())
	defer cancel()
	out, err := (RetryLLM{Client: e.LLMClient, Retries: e.Config.LLMRetries, Backoff: e.Config.RetryBackoff}).Generate(ctx, "评估以下测试结果的用例质量，只返回 0 到 100 的数字："+string(payload))
	if err != nil {
		return 0, false
	}
	var score float64
	if _, err := fmt.Sscanf(strings.TrimSpace(out), "%f", &score); err != nil {
		match := regexp.MustCompile(`\d+(?:\.\d+)?`).FindString(out)
		if match == "" {
			return 0, false
		}
		if _, err := fmt.Sscanf(match, "%f", &score); err != nil {
			return 0, false
		}
	}
	return clamp(score, 0, 100), true
}

func clamp(v, min, max float64) float64 {
	if v < min {
		return min
	}
	if v > max {
		return max
	}
	return v
}

func RenderQualityReport(report QualityReport) string {
	var b strings.Builder
	fmt.Fprintf(&b, "智能测试质量报告\n总用例：%d，已执行：%d，通过：%d，失败：%d\n覆盖率：%.1f%%\n缺陷检测率：%.1f%%\n用例质量评分：%.1f/100\n%s", report.TotalCases, report.ExecutedCases, report.PassedCases, report.FailedCases, report.CoverageRate*100, report.DefectDetectionRate*100, report.CaseQualityScore, report.Summary)
	for _, rec := range report.Recommendations {
		b.WriteString("\n- ")
		b.WriteString(rec)
	}
	return b.String()
}
