#include "PluginWebPanel.hpp"

#include "Plater.hpp"
#include "Widgets/WebView.hpp"
#include "Widgets/WebViewHostDialog.hpp"
#include "slic3r/GUI/GUI.hpp"
#include "slic3r/GUI/GUI_App.hpp"

#include <libslic3r/Utils.hpp>

#include <boost/filesystem.hpp>
#include <boost/log/trivial.hpp>

#include <wx/app.h>
#include <wx/event.h>
#include <wx/log.h>
#include <wx/sizer.h>

#include <utility>

namespace Slic3r { namespace GUI {

namespace {

// Same contract as PluginWebDialog's bridge: window.orca is the only host
// surface the page may use. Duplicated rather than shared because the dialog's
// copy is file-local and that file is not ours to touch; keep both in sync.
constexpr char ORCA_BRIDGE_JS[] = R"JS(
(function () {
  if (window.top !== window.self) return;
  if (window.orca) return;
  var handlers = [];
  function send(kind, data) {
    try {
      window.wx.postMessage(JSON.stringify({
        channel: 'orca', kind: kind, data: (data === undefined ? null : data)
      }));
    } catch (e) { /* bridge not ready yet */ }
  }
  window.orca = {
    postMessage: function (d) { send('message', d); },
    submit:      function (d) { send('submit', d); },
    close:       function ()  { send('close'); },
    onMessage:   function (cb) { if (typeof cb === 'function') handlers.push(cb); }
  };
  window.__orcaDispatch = function (payload) {
    var data = payload ? payload.data : null;
    for (var i = 0; i < handlers.length; i++) {
      try { handlers[i](data); } catch (e) {}
    }
  };
})();
)JS";

// file:// base URL for plugin HTML loaded via SetPage, so relative URLs in the
// page resolve against the bundled web resources directory.
wxString web_base_url()
{
    const std::string dir = (boost::filesystem::path(resources_dir()) / "web").make_preferred().string();
    return wxString("file://") + from_u8(dir) + "/";
}

// Live re-theme after WebView::RecreateAll (app theme toggle). Compact copy of
// WebViewHostDialog's file-local theme helpers: rebuild the :root variables
// from the live theme and swap them into the already-loaded document.
std::string live_theme_apply_js()
{
    GUI_App&        app    = wxGetApp();
    const wxString  bg     = app.get_window_default_clr().GetAsString(wxC2S_HTML_SYNTAX);
    const wxString  fg     = app.get_label_clr_default().GetAsString(wxC2S_HTML_SYNTAX);
    const wxString  muted  = app.get_label_clr_sys().GetAsString(wxC2S_HTML_SYNTAX);
    const wxString  border = app.get_highlight_default_clr().GetAsString(wxC2S_HTML_SYNTAX);
    const wxString  accent = wxColour("#009688").GetAsString(wxC2S_HTML_SYNTAX);
    const wxString  theme  = app.dark_mode() ? wxString("dark") : wxString("light");

    std::string css = ":root{--orca-bg:" + bg.ToStdString() + ";--orca-fg:" + fg.ToStdString() +
                      ";--orca-muted:" + muted.ToStdString() + ";--orca-border:" + border.ToStdString() +
                      ";--orca-accent:" + accent.ToStdString() + ";}";
    const std::string vars_literal = nlohmann::json(css).dump();
    return "(function(){var css=" + vars_literal + ";var theme=\"" + theme.ToStdString() + "\";" +
           "var el=document.getElementById('orca-host-theme-vars');"
           "if(el){el.textContent=css;}"
           "if(document.documentElement)document.documentElement.setAttribute('data-orca-theme',theme);})();";
}

} // namespace

PluginWebPanel::PluginWebPanel(wxWindow*         parent,
                               const std::string& html,
                               MessageHandler     on_message,
                               CloseHandler       on_close,
                               CloseHandler       on_destroyed)
    : wxPanel(parent, wxID_ANY)
    , m_html(html)
    , m_on_message(std::move(on_message))
    , m_on_close(std::move(on_close))
    , m_on_destroyed(std::move(on_destroyed))
{
    // Same startup sequence as PluginWebDialog: a bundled bootstrap page brings
    // the webview up, user scripts are registered before the (re)load, and the
    // real plugin HTML is swapped in via SetPage once the bootstrap settles.
    const wxString bootstrap = wxString("file://") + from_u8(
        (boost::filesystem::path(resources_dir()) / "web/dialog/PluginWebDialog/blank.html").make_preferred().string());

    SetBackgroundColour(wxGetApp().get_window_default_clr());

    m_browser = WebView::CreateWebView(this, bootstrap);
    if (m_browser == nullptr) {
        wxLogError("Could not create the plugin side panel webview");
        return;
    }

    auto* topsizer = new wxBoxSizer(wxVERTICAL);
    SetSizer(topsizer);
    topsizer->Add(m_browser, wxSizerFlags().Expand().Proportion(1));

    m_browser->SetBackgroundColour(wxGetApp().get_window_default_clr());
    // Theme contract first, then plugin defaults, then the orca bridge — same
    // order as WebViewHostDialog::register_theme_user_scripts() + add_user_scripts().
    m_browser->AddUserScript(wxString::FromUTF8(WebViewHostDialog::theme_user_script()));
    m_browser->AddUserScript(wxString::FromUTF8(WebViewHostDialog::plugin_defaults_user_script()));
    m_browser->AddUserScript(wxString::FromUTF8(ORCA_BRIDGE_JS));

    Bind(wxEVT_WEBVIEW_LOADED, &PluginWebPanel::on_bootstrap_event, this, m_browser->GetId());
    Bind(wxEVT_WEBVIEW_ERROR, &PluginWebPanel::on_bootstrap_event, this, m_browser->GetId());
    Bind(wxEVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED, &PluginWebPanel::on_script_message_event, this, m_browser->GetId());
    // Theme toggle: re-theme in place and do NOT Skip(), so WebView::RecreateAll
    // skips the redundant reload (same contract as WebViewHostDialog).
    m_browser->Bind(EVT_WEBVIEW_RECREATED, [this](wxCommandEvent&) {
        if (m_browser != nullptr)
            WebView::RunScript(m_browser, wxString::FromUTF8(live_theme_apply_js()));
    });

    WebView::LoadUrl(m_browser, bootstrap);
}

PluginWebPanel::~PluginWebPanel()
{
    // Runs on every destruction path and only touches host-side state (no Python).
    if (m_on_destroyed)
        m_on_destroyed();
}

void PluginWebPanel::post_message(PluginWebPanel* panel, const nlohmann::json& data)
{
    if (panel != nullptr)
        panel->push_message(data);
}

void PluginWebPanel::request_close(PluginWebPanel* panel)
{
    if (panel == nullptr)
        return;
    panel->fire_close();
    panel->close_pane_after();
}

void PluginWebPanel::push_message(const nlohmann::json& data)
{
    if (m_browser == nullptr)
        return;
    nlohmann::json envelope;
    envelope["data"] = data;
    const wxString script = wxT("__orcaDispatch(") +
        wxString::FromUTF8(envelope.dump(-1, ' ', false, nlohmann::json::error_handler_t::ignore)) + wxT(")");
    wxGetApp().CallAfter([this, script]() {
        if (m_browser != nullptr)
            WebView::RunScript(m_browser, script);
    });
}

void PluginWebPanel::on_bootstrap_event(wxWebViewEvent& event)
{
    load_plugin_content();
    event.Skip();
}

void PluginWebPanel::load_plugin_content()
{
    if (m_content_loaded || m_browser == nullptr)
        return;
    m_content_loaded = true;
    m_browser->SetPage(wxString::FromUTF8(m_html), web_base_url());
}

void PluginWebPanel::on_script_message_event(wxWebViewEvent& event)
{
    const wxString payload = event.GetString();
    try {
        on_script_message(nlohmann::json::parse(payload.utf8_string()));
    } catch (const std::exception& e) {
        BOOST_LOG_TRIVIAL(trace) << "PluginWebPanel: script message parse error: " << e.what();
    }
}

void PluginWebPanel::on_script_message(const nlohmann::json& payload)
{
    if (payload.value("channel", std::string()) == "orca") {
        const std::string    kind = payload.value("kind", std::string());
        const nlohmann::json data = payload.contains("data") ? payload["data"] : nlohmann::json();
        if (kind == "message") {
            if (m_on_message)
                m_on_message(data);
        } else if (kind == "close") {
            // Page-initiated close: same path as the pane's close button.
            request_close(this);
        }
        // "submit" is dialog-only; a panel page has no submit consumer.
        return;
    }

    const std::string command = payload.value("command", "");
    if (command == "close_page")
        request_close(this);
}

void PluginWebPanel::fire_close()
{
    if (m_close_fired)
        return;
    m_close_fired = true;
    if (m_on_close) {
        CloseHandler cb = m_on_close;
        m_on_close      = nullptr;
        cb();
    }
}

void PluginWebPanel::close_pane_after()
{
    // Never tear the pane down from inside a webview script callback or an AUI
    // event: defer to a clean main-loop iteration.
    const wxString name = m_pane_name;
    wxTheApp->CallAfter([name]() {
        Plater* plater = GUI::wxGetApp().plater();
        if (plater != nullptr)
            plater->undock_plugin_side_panel(name);
    });
}

}} // namespace Slic3r::GUI
