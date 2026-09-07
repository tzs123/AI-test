{{/*
Common labels
*/}}
{{- define "runnergo.labels" -}}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Global env from config.env
*/}}
{{- define "runnergo.globalEnv" -}}
{{- range $k, $v := .Values.configEnv -}}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end -}}
{{- end -}}