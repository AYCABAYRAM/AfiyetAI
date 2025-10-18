// Main JavaScript for AfiyetAI

document.addEventListener('DOMContentLoaded', function() {
    // Initialize tooltips
    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });

    // File upload preview
    const fileInput = document.getElementById('file');
    if (fileInput) {
        fileInput.addEventListener('change', function(e) {
            const file = e.target.files[0];
            if (file) {
                // Show file info
                const fileInfo = document.createElement('div');
                fileInfo.className = 'alert alert-info mt-2';
                fileInfo.innerHTML = `
                    <i class="fas fa-file-image me-2"></i>
                    <strong>Seçilen dosya:</strong> ${file.name} (${formatFileSize(file.size)})
                `;
                
                // Remove existing file info
                const existingInfo = document.querySelector('.file-info');
                if (existingInfo) {
                    existingInfo.remove();
                }
                
                fileInfo.className += ' file-info';
                fileInput.parentNode.appendChild(fileInfo);
            }
        });
    }

    // Form validation
    const uploadForm = document.getElementById('uploadForm');
    if (uploadForm) {
        uploadForm.addEventListener('submit', function(e) {
            const fileInput = document.getElementById('file');
            if (!fileInput.files.length) {
                e.preventDefault();
                showAlert('Lütfen bir dosya seçin!', 'warning');
                return false;
            }

            const file = fileInput.files[0];
            const maxSize = 16 * 1024 * 1024; // 16MB
            
            if (file.size > maxSize) {
                e.preventDefault();
                showAlert('Dosya boyutu 16MB\'dan büyük olamaz!', 'danger');
                return false;
            }

            // Show loading state
            showLoadingState();
        });
    }

    // Auto-hide alerts
    setTimeout(function() {
        const alerts = document.querySelectorAll('.alert');
        alerts.forEach(function(alert) {
            if (alert.classList.contains('alert-success') || alert.classList.contains('alert-info')) {
                const bsAlert = new bootstrap.Alert(alert);
                bsAlert.close();
            }
        });
    }, 5000);

    // Smooth scrolling for anchor links
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function (e) {
            e.preventDefault();
            const target = document.querySelector(this.getAttribute('href'));
            if (target) {
                target.scrollIntoView({
                    behavior: 'smooth',
                    block: 'start'
                });
            }
        });
    });

    // Add fade-in animation to cards
    const cards = document.querySelectorAll('.card');
    cards.forEach((card, index) => {
        card.style.animationDelay = `${index * 0.1}s`;
        card.classList.add('fade-in-up');
    });
});

// Utility functions
function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function showAlert(message, type = 'info') {
    const alertContainer = document.createElement('div');
    alertContainer.className = `alert alert-${type} alert-dismissible fade show`;
    alertContainer.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    `;
    
    // Insert at the top of the page
    const container = document.querySelector('.container');
    if (container) {
        container.insertBefore(alertContainer, container.firstChild);
    }
    
    // Auto-hide after 5 seconds
    setTimeout(() => {
        const bsAlert = new bootstrap.Alert(alertContainer);
        bsAlert.close();
    }, 5000);
}

function showLoadingState() {
    const uploadBtn = document.getElementById('uploadBtn');
    const progressContainer = document.getElementById('progressContainer');
    const progressBar = document.querySelector('.progress-bar');
    const progressText = document.getElementById('progressText');
    
    if (uploadBtn) {
        uploadBtn.disabled = true;
        uploadBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>İşleniyor...';
    }
    
    if (progressContainer) {
        progressContainer.style.display = 'block';
        
        // Simulate progress
        let progress = 0;
        const interval = setInterval(() => {
            progress += Math.random() * 15;
            if (progress > 90) progress = 90;
            
            if (progressBar) {
                progressBar.style.width = progress + '%';
            }
            
            if (progressText) {
                if (progress < 30) {
                    progressText.textContent = 'Fiş yükleniyor...';
                } else if (progress < 60) {
                    progressText.textContent = 'OCR işlemi yapılıyor...';
                } else if (progress < 90) {
                    progressText.textContent = 'Ürünler analiz ediliyor...';
                } else {
                    progressText.textContent = 'Tarifler hazırlanıyor...';
                }
            }
        }, 500);
        
        // Clear interval after 10 seconds
        setTimeout(() => {
            clearInterval(interval);
        }, 10000);
    }
}

// API functions
async function processReceiptAPI(file) {
    const formData = new FormData();
    formData.append('file', file);
    
    try {
        const response = await fetch('/api/process', {
            method: 'POST',
            body: formData
        });
        
        const result = await response.json();
        return result;
    } catch (error) {
        console.error('API Error:', error);
        return { success: false, error: 'API hatası' };
    }
}

// Copy to clipboard function
function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(function() {
        showAlert('Panoya kopyalandı!', 'success');
    }, function(err) {
        console.error('Copy failed: ', err);
        showAlert('Kopyalama başarısız!', 'danger');
    });
}

// Print function
function printResults() {
    window.print();
}

// Export function
function exportResults() {
    const data = {
        timestamp: new Date().toISOString(),
        products: [],
        recommendations: []
    };
    
    // Collect product data
    document.querySelectorAll('.table tbody tr').forEach(row => {
        const cells = row.querySelectorAll('td');
        if (cells.length >= 3) {
            data.products.push({
                name: cells[0].textContent.trim(),
                price: cells[1].textContent.trim(),
                status: cells[2].textContent.trim()
            });
        }
    });
    
    // Collect recommendation data
    document.querySelectorAll('.recipe-card').forEach(card => {
        const title = card.querySelector('.card-title');
        const priority = card.querySelector('.badge');
        const usedProducts = card.querySelectorAll('.badge.bg-success');
        
        if (title) {
            data.recommendations.push({
                title: title.textContent.trim(),
                priority: priority ? priority.textContent.trim() : '',
                usedProducts: Array.from(usedProducts).map(badge => badge.textContent.trim())
            });
        }
    });
    
    // Download as JSON
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `afiyet-ai-results-${new Date().toISOString().split('T')[0]}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    
    showAlert('Sonuçlar dışa aktarıldı!', 'success');
}

