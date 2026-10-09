{{/* Chart name, used as the app.kubernetes.io/name label. */}}
{{- define "store.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Base name for every resource. A release called "store" yields "store-wordpress"
and "store-mariadb" rather than "store-store-wordpress".
*/}}
{{- define "store.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "store.wordpress.fullname" -}}
{{- printf "%s-wordpress" (include "store.fullname" .) }}
{{- end }}

{{- define "store.mariadb.fullname" -}}
{{- printf "%s-mariadb" (include "store.fullname" .) }}
{{- end }}

{{- define "store.wooMcp.fullname" -}}
{{- printf "%s-woo-mcp" (include "store.fullname" .) }}
{{- end }}

{{/* Labels on every object. */}}
{{- define "store.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "store.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Labels that identify this release's pods. Selectors add
app.kubernetes.io/component to pick one workload.
*/}}
{{- define "store.selectorLabels" -}}
app.kubernetes.io/name: {{ include "store.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "store.secretName" -}}
{{- .Values.secrets.existingSecret | default (printf "%s-secrets" (include "store.fullname" .)) }}
{{- end }}

{{/* The store's public URL. TLS on the Ingress is what makes it https. */}}
{{- define "store.url" -}}
{{- printf "%s://%s" (ternary "https" "http" .Values.ingress.tls.enabled) .Values.store.host }}
{{- end }}

{{- define "store.wordpress.image" -}}
{{- $image := .Values.wordpress.image }}
{{- $repository := ternary (printf "%s/%s" $image.registry $image.repository) $image.repository (not (empty $image.registry)) }}
{{- printf "%s:%s" $repository ($image.tag | default .Chart.AppVersion) }}
{{- end }}

{{- define "store.wooMcp.image" -}}
{{- $image := .Values.wooMcp.image }}
{{- $repository := ternary (printf "%s/%s" $image.registry $image.repository) $image.repository (not (empty $image.registry)) }}
{{- printf "%s:%s" $repository $image.tag }}
{{- end }}

{{/*
Every name a caller inside the cluster can use for the MCP server's Service,
plus the loopback names `kubectl port-forward` produces. The server answers
only requests whose Host header is one of these.
*/}}
{{- define "store.wooMcp.allowedHosts" -}}
{{- $name := include "store.wooMcp.fullname" . }}
{{- $namespace := .Release.Namespace }}
{{- list $name (printf "%s.%s" $name $namespace) (printf "%s.%s.svc" $name $namespace) (printf "%s.%s.svc.%s" $name $namespace .Values.clusterDomain) "localhost" "127.0.0.1" | join "," }}
{{- end }}

{{/*
Environment that wp-config.php needs on every request: where the database is,
the keys that sign cookies, and the public URL. Shared by the web container and
the setup init container.
*/}}
{{- define "store.wordpress.env" -}}
- name: STORE_URL
  value: {{ include "store.url" . | quote }}
- name: WORDPRESS_DB_HOST
  value: {{ include "store.mariadb.fullname" . | quote }}
- name: WORDPRESS_DB_NAME
  value: {{ .Values.mariadb.database | quote }}
- name: WORDPRESS_DB_USER
  value: {{ .Values.mariadb.user | quote }}
- name: WORDPRESS_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "store.secretName" . }}
      key: mariadb-password
{{- range $key := list "auth-key" "secure-auth-key" "logged-in-key" "nonce-key" "auth-salt" "secure-auth-salt" "logged-in-salt" "nonce-salt" }}
- name: WORDPRESS_{{ $key | upper | replace "-" "_" }}
  valueFrom:
    secretKeyRef:
      name: {{ include "store.secretName" $ }}
      key: {{ $key }}
{{- end }}
- name: WORDPRESS_DEBUG
  value: {{ ternary "1" "0" .Values.wordpress.debug | quote }}
{{- end }}

{{/*
Pod-level security settings shared by every pod that runs the WordPress image.
33 is www-data in that image.
*/}}
{{- define "store.wordpress.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 33
runAsGroup: 33
fsGroup: 33
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{/* Container-level security settings shared by every container in the chart. */}}
{{- define "store.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop:
    - ALL
{{- end }}

{{/*
An HTTP probe that loads a real WordPress page, dressed as a request for the
public URL so WordPress answers it instead of redirecting.
*/}}
{{- define "store.wordpress.pageProbe" -}}
path: /wp-login.php
port: http
httpHeaders:
  - name: Host
    value: {{ .Values.store.host | quote }}
  - name: X-Forwarded-Proto
    value: {{ ternary "https" "http" .Values.ingress.tls.enabled }}
{{- end }}
