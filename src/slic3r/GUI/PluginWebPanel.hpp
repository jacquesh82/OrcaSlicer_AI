#ifndef slic3r_GUI_PluginWebPanel_hpp_
#define slic3r_GUI_PluginWebPanel_hpp_

#include <functional>
#include <string>

#include <nlohmann/json.hpp>
#include <wx/panel.h>
#include <wx/string.h>
#include <wx/webview.h>

namespace Slic3r { namespace GUI {

// Docked sibling of PluginWebDialog: same window.orca bridge and message flow,
// but as a wxPanel meant to be added to the plater's wxAuiManager as a side
// pane (Plater::dock_plugin_side_panel) instead of a top-level dialog.
//
// Like PluginWebDialog it is Python-agnostic: hooks are std::function and must
// not capture bare pybind11 objects (the panel can be destroyed on the main
// thread without the GIL); the plugin layer wraps Python callables GIL-safely.
//
// Close semantics mirror the dialog:
//   - request_close()/page-initiated window.orca.close(): fires on_close once
//     (user-style close); the host then undocks and destroys the pane.
//   - destroy_for_plugin()/plugin unload: no on_close, only on_destroyed,
//     which runs from the destructor on every path and must stay GIL-free.
class PluginWebPanel : public wxPanel
{
public:
    using MessageHandler = std::function<void(const nlohmann::json& data)>;
    using CloseHandler   = std::function<void()>;

    PluginWebPanel(wxWindow*      parent,
                   const std::string& html,
                   MessageHandler on_message,
                   CloseHandler   on_close,
                   CloseHandler   on_destroyed);
    ~PluginWebPanel() override;

    PluginWebPanel(const PluginWebPanel&) = delete;
    PluginWebPanel& operator=(const PluginWebPanel&) = delete;

    // Null-safe entry points used by the plugin binding layer.
    static void post_message(PluginWebPanel* panel, const nlohmann::json& data);
    static void request_close(PluginWebPanel* panel);

    // Push a payload to the page; delivered to window.orca.onMessage handlers.
    // MAIN-THREAD ONLY is not required (marshals through CallAfter itself).
    void push_message(const nlohmann::json& data);

    // The AUI pane name assigned at dock time ("plugin_panel_<id>"); used to
    // undock the pane when this panel is closed or torn down.
    const wxString& pane_name() const { return m_pane_name; }
    void            set_pane_name(const wxString& name) { m_pane_name = name; }

private:
    void on_bootstrap_event(wxWebViewEvent& event);
    void load_plugin_content();
    void on_script_message_event(wxWebViewEvent& event);
    void on_script_message(const nlohmann::json& payload);
    void fire_close();
    void close_pane_after(); // undock + destroy, deferred out of webview callbacks

    std::string    m_html;
    bool           m_content_loaded{false};
    bool           m_close_fired{false};
    wxString       m_pane_name;
    wxWebView*     m_browser{nullptr};
    MessageHandler m_on_message;
    CloseHandler   m_on_close;
    CloseHandler   m_on_destroyed;
};

}} // namespace Slic3r::GUI

#endif // slic3r_GUI_PluginWebPanel_hpp_
