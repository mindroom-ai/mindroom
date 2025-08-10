import { useState } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  CheckCircle2,
  Copy,
  ExternalLink,
  Loader2,
  Terminal,
  Wand2,
  AlertCircle,
  ChevronRight,
} from 'lucide-react';
import { useToast } from '@/components/ui/use-toast';

interface SetupStep {
  id: string;
  title: string;
  description: string;
  completed: boolean;
}

export function GoogleSetupWizard() {
  const [currentStep, setCurrentStep] = useState(0);
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [projectId] = useState('mindroom-integration');
  const [loading, setLoading] = useState(false);
  const { toast } = useToast();

  const steps: SetupStep[] = [
    {
      id: 'intro',
      title: 'Welcome',
      description: 'Set up Google Cloud for Gmail access',
      completed: false,
    },
    {
      id: 'project',
      title: 'Create Project',
      description: 'Create a Google Cloud project',
      completed: false,
    },
    {
      id: 'apis',
      title: 'Enable APIs',
      description: 'Enable Gmail and other APIs',
      completed: false,
    },
    {
      id: 'oauth',
      title: 'OAuth Setup',
      description: 'Configure OAuth credentials',
      completed: false,
    },
    {
      id: 'complete',
      title: 'Complete',
      description: 'Save credentials',
      completed: false,
    },
  ];

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    toast({
      title: 'Copied!',
      description: 'Command copied to clipboard',
    });
  };

  const handleQuickSetup = async () => {
    setLoading(true);
    try {
      // Get the setup script
      const response = await fetch('http://localhost:8000/api/setup/google/quick-setup-script');
      const data = await response.json();

      // Create a blob and download
      const blob = new Blob([data.script], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = data.filename;
      a.click();

      toast({
        title: 'Script Downloaded',
        description: 'Run the script in your terminal to set up Google Cloud',
      });
    } catch (error) {
      toast({
        title: 'Error',
        description: 'Failed to generate setup script',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleSaveCredentials = async () => {
    if (!clientId || !clientSecret) {
      toast({
        title: 'Missing Information',
        description: 'Please enter both Client ID and Client Secret',
        variant: 'destructive',
      });
      return;
    }

    setLoading(true);
    try {
      const response = await fetch('http://localhost:8000/api/setup/google/complete-setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          client_id: clientId,
          client_secret: clientSecret,
          project_id: projectId,
        }),
      });

      if (response.ok) {
        toast({
          title: 'Success!',
          description: 'Google OAuth credentials saved. Agents can now use Gmail!',
        });
        setCurrentStep(4);
      }
    } catch (error) {
      toast({
        title: 'Error',
        description: 'Failed to save credentials',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Wand2 className="h-5 w-5" />
          Google Cloud Setup Wizard
        </CardTitle>
        <CardDescription>One-time setup to enable Gmail access for your agents</CardDescription>
      </CardHeader>
      <CardContent>
        <Tabs value="guided" className="space-y-4">
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="guided">Guided Setup</TabsTrigger>
            <TabsTrigger value="quick">Quick Setup</TabsTrigger>
          </TabsList>

          <TabsContent value="guided" className="space-y-4">
            {/* Step Progress */}
            <div className="flex items-center justify-between mb-6">
              {steps.map((step, idx) => (
                <div key={step.id} className="flex items-center">
                  <div
                    className={`
                    w-8 h-8 rounded-full flex items-center justify-center text-sm
                    ${
                      idx <= currentStep
                        ? 'bg-primary text-primary-foreground'
                        : 'bg-muted text-muted-foreground'
                    }
                  `}
                  >
                    {idx < currentStep ? <CheckCircle2 className="h-4 w-4" /> : idx + 1}
                  </div>
                  {idx < steps.length - 1 && (
                    <div
                      className={`w-16 h-0.5 mx-2 ${idx < currentStep ? 'bg-primary' : 'bg-muted'}`}
                    />
                  )}
                </div>
              ))}
            </div>

            {/* Step Content */}
            {currentStep === 0 && (
              <div className="space-y-4">
                <Alert>
                  <AlertCircle className="h-4 w-4" />
                  <AlertTitle>One-Time Setup Required</AlertTitle>
                  <AlertDescription>
                    This wizard will help you set up Google Cloud so your AI agents can access
                    Gmail. You only need to do this once, and it takes about 5 minutes.
                  </AlertDescription>
                </Alert>

                <div className="space-y-2">
                  <h3 className="font-medium">What we'll do:</h3>
                  <ul className="list-disc ml-6 space-y-1 text-sm text-muted-foreground">
                    <li>Create a Google Cloud project</li>
                    <li>Enable Gmail API</li>
                    <li>Create OAuth credentials</li>
                    <li>Save credentials for your agents</li>
                  </ul>
                </div>

                <Button onClick={() => setCurrentStep(1)} className="w-full">
                  Get Started <ChevronRight className="ml-2 h-4 w-4" />
                </Button>
              </div>
            )}

            {currentStep === 1 && (
              <div className="space-y-4">
                <h3 className="font-medium">Step 1: Create Google Cloud Project</h3>

                <Alert>
                  <Terminal className="h-4 w-4" />
                  <AlertTitle>Option A: Use Terminal (Recommended)</AlertTitle>
                  <AlertDescription className="space-y-2 mt-2">
                    <p>Run these commands in your terminal:</p>
                    <div className="bg-secondary p-2 rounded mt-2 font-mono text-xs">
                      <div className="flex justify-between items-center">
                        <span>gcloud projects create {projectId}</span>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => copyToClipboard(`gcloud projects create ${projectId}`)}
                        >
                          <Copy className="h-3 w-3" />
                        </Button>
                      </div>
                    </div>
                  </AlertDescription>
                </Alert>

                <Alert>
                  <ExternalLink className="h-4 w-4" />
                  <AlertTitle>Option B: Use Web Console</AlertTitle>
                  <AlertDescription className="space-y-2 mt-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        window.open('https://console.cloud.google.com/projectcreate', '_blank')
                      }
                    >
                      Open Google Cloud Console <ExternalLink className="ml-2 h-3 w-3" />
                    </Button>
                    <p className="text-xs mt-2">
                      Create a project named: <strong>{projectId}</strong>
                    </p>
                  </AlertDescription>
                </Alert>

                <div className="flex gap-2">
                  <Button variant="outline" onClick={() => setCurrentStep(0)}>
                    Back
                  </Button>
                  <Button onClick={() => setCurrentStep(2)} className="flex-1">
                    Project Created <ChevronRight className="ml-2 h-4 w-4" />
                  </Button>
                </div>
              </div>
            )}

            {currentStep === 2 && (
              <div className="space-y-4">
                <h3 className="font-medium">Step 2: Enable APIs</h3>

                <Alert>
                  <Terminal className="h-4 w-4" />
                  <AlertTitle>Run these commands:</AlertTitle>
                  <AlertDescription className="space-y-2 mt-2">
                    {[
                      `gcloud config set project ${projectId}`,
                      'gcloud services enable gmail.googleapis.com',
                      'gcloud services enable calendar-json.googleapis.com',
                      'gcloud services enable drive.googleapis.com',
                    ].map(cmd => (
                      <div key={cmd} className="bg-secondary p-2 rounded font-mono text-xs">
                        <div className="flex justify-between items-center">
                          <span>{cmd}</span>
                          <Button size="sm" variant="ghost" onClick={() => copyToClipboard(cmd)}>
                            <Copy className="h-3 w-3" />
                          </Button>
                        </div>
                      </div>
                    ))}
                  </AlertDescription>
                </Alert>

                <div className="flex gap-2">
                  <Button variant="outline" onClick={() => setCurrentStep(1)}>
                    Back
                  </Button>
                  <Button onClick={() => setCurrentStep(3)} className="flex-1">
                    APIs Enabled <ChevronRight className="ml-2 h-4 w-4" />
                  </Button>
                </div>
              </div>
            )}

            {currentStep === 3 && (
              <div className="space-y-4">
                <h3 className="font-medium">Step 3: Create OAuth Credentials</h3>

                <Alert>
                  <ExternalLink className="h-4 w-4" />
                  <AlertTitle>Manual step required (Google's security policy)</AlertTitle>
                  <AlertDescription className="space-y-2 mt-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        window.open(
                          `https://console.cloud.google.com/apis/credentials?project=${projectId}`,
                          '_blank'
                        )
                      }
                    >
                      Open Credentials Page <ExternalLink className="ml-2 h-3 w-3" />
                    </Button>
                  </AlertDescription>
                </Alert>

                <div className="space-y-3 text-sm">
                  <div className="p-3 bg-muted rounded">
                    <p className="font-medium mb-1">1. Configure Consent Screen:</p>
                    <ul className="ml-4 space-y-1 text-muted-foreground">
                      <li>
                        • User Type: <strong>External</strong>
                      </li>
                      <li>
                        • App Name: <strong>MindRoom</strong>
                      </li>
                      <li>
                        • Support Email: <strong>Your email</strong>
                      </li>
                    </ul>
                  </div>

                  <div className="p-3 bg-muted rounded">
                    <p className="font-medium mb-1">2. Create OAuth Client ID:</p>
                    <ul className="ml-4 space-y-1 text-muted-foreground">
                      <li>
                        • Type: <strong>Web application</strong>
                      </li>
                      <li>
                        • Name: <strong>MindRoom Web</strong>
                      </li>
                      <li>
                        • Redirect URI:{' '}
                        <strong>http://localhost:8000/api/auth/google/callback</strong>
                      </li>
                    </ul>
                  </div>

                  <div className="p-3 bg-muted rounded">
                    <p className="font-medium mb-1">3. Copy your credentials:</p>
                    <div className="space-y-2 mt-2">
                      <div>
                        <Label htmlFor="client-id">Client ID</Label>
                        <Input
                          id="client-id"
                          placeholder="Paste your Client ID here"
                          value={clientId}
                          onChange={e => setClientId(e.target.value)}
                        />
                      </div>
                      <div>
                        <Label htmlFor="client-secret">Client Secret</Label>
                        <Input
                          id="client-secret"
                          type="password"
                          placeholder="Paste your Client Secret here"
                          value={clientSecret}
                          onChange={e => setClientSecret(e.target.value)}
                        />
                      </div>
                    </div>
                  </div>
                </div>

                <div className="flex gap-2">
                  <Button variant="outline" onClick={() => setCurrentStep(2)}>
                    Back
                  </Button>
                  <Button
                    onClick={handleSaveCredentials}
                    className="flex-1"
                    disabled={!clientId || !clientSecret || loading}
                  >
                    {loading ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Saving...
                      </>
                    ) : (
                      <>
                        Save Credentials <ChevronRight className="ml-2 h-4 w-4" />
                      </>
                    )}
                  </Button>
                </div>
              </div>
            )}

            {currentStep === 4 && (
              <div className="space-y-4">
                <div className="text-center py-8">
                  <CheckCircle2 className="h-16 w-16 text-green-600 mx-auto mb-4" />
                  <h3 className="text-xl font-semibold mb-2">Setup Complete!</h3>
                  <p className="text-muted-foreground">
                    Your AI agents can now access Gmail on your behalf.
                  </p>
                </div>

                <Alert className="bg-green-50 dark:bg-green-950">
                  <CheckCircle2 className="h-4 w-4 text-green-600" />
                  <AlertTitle>What you can do now:</AlertTitle>
                  <AlertDescription>
                    <ul className="mt-2 space-y-1 text-sm">
                      <li>• Ask agents to check your email</li>
                      <li>• Have agents send emails for you</li>
                      <li>• Search emails by content or sender</li>
                      <li>• Create drafts and manage your inbox</li>
                    </ul>
                  </AlertDescription>
                </Alert>

                <Button variant="outline" onClick={() => setCurrentStep(0)} className="w-full">
                  Start Over
                </Button>
              </div>
            )}
          </TabsContent>

          <TabsContent value="quick" className="space-y-4">
            <Alert>
              <Terminal className="h-4 w-4" />
              <AlertTitle>Quick Setup Script</AlertTitle>
              <AlertDescription>
                Download and run a script that automates most of the setup process. You'll still
                need to manually create OAuth credentials at the end.
              </AlertDescription>
            </Alert>

            <div className="space-y-4">
              <Button onClick={handleQuickSetup} disabled={loading} className="w-full">
                {loading ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Generating...
                  </>
                ) : (
                  <>
                    Download Setup Script <Terminal className="ml-2 h-4 w-4" />
                  </>
                )}
              </Button>

              <div className="text-sm text-muted-foreground space-y-2">
                <p>After downloading:</p>
                <ol className="list-decimal ml-6 space-y-1">
                  <li>
                    Run: <code className="bg-muted px-1 rounded">chmod +x setup_google.sh</code>
                  </li>
                  <li>
                    Run: <code className="bg-muted px-1 rounded">./setup_google.sh</code>
                  </li>
                  <li>Follow the instructions in the script</li>
                  <li>Come back here to save your credentials</li>
                </ol>
              </div>

              <div className="border-t pt-4">
                <h4 className="font-medium mb-2">Already have credentials?</h4>
                <div className="space-y-2">
                  <Input
                    placeholder="Client ID"
                    value={clientId}
                    onChange={e => setClientId(e.target.value)}
                  />
                  <Input
                    type="password"
                    placeholder="Client Secret"
                    value={clientSecret}
                    onChange={e => setClientSecret(e.target.value)}
                  />
                  <Button
                    onClick={handleSaveCredentials}
                    disabled={!clientId || !clientSecret || loading}
                    className="w-full"
                  >
                    Save Credentials
                  </Button>
                </div>
              </div>
            </div>
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  );
}
